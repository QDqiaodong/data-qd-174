"""Internal cost comparison of supplier annexes against one service basket.

A bid annex quotes line-level price *ranges* plus two sets of subtotals (by
service site and by service category).  Several concrete line allocations can
therefore share one contract total.  This module never treats one arbitrary
allocation as fact: it first checks each annex for self-consistency, then, for
annexes whose basket quantities match exactly, computes the minimum and
maximum achievable basket totals, each reached by its own complete legal
allocation.

The feasibility and extremum problems are bounded transportation problems:

* each line amount ``x`` is an integer with ``min_cents <= x <= max_cents``;
* line amounts at one site sum to that site's subtotal;
* line amounts in one service category sum to that category's subtotal;
* both subtotal grand totals must equal the reviewed offer price.

They are solved as integer min-cost flow on a bipartite network using exact
:class:`int` arithmetic only (Python 3.11+ standard library).  When the
subtotals agree with the offer price but no allocation exists, a violating
Hoffman cut (site set ``S``, category set ``T``) is reported, proving from the
subtotals and line bounds alone that they cannot hold simultaneously.
"""

from __future__ import annotations

import json
import math
from collections import deque
from fractions import Fraction
from pathlib import Path

__all__ = [
    "CostCompareError",
    "compare_bid_costs",
    "load_json_file",
    "verify_allocation",
]


class CostCompareError(ValueError):
    """Cost comparison input is invalid.  No result file may be produced."""


# ---------------------------------------------------------------------------
# Strict JSON loading
# ---------------------------------------------------------------------------


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CostCompareError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise CostCompareError(f"non-finite JSON number {name} is not allowed")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise CostCompareError(f"non-finite JSON number {text} is not allowed")
    return value


def parse_json_text(text, source):
    """Parse strict JSON, rejecting duplicate keys and non-finite numbers."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except CostCompareError as exc:
        raise CostCompareError(f"{source}: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise CostCompareError(f"{source}: invalid JSON: {exc}") from exc


def load_json_file(path):
    """Read and strictly parse one local JSON file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return parse_json_text(text, str(path))


def _reject_floats(value, field):
    if isinstance(value, float):
        raise CostCompareError(f"{field} must be an integer, got a decimal number")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, f"{field}[{index}]")


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _non_empty_string(value, field):
    if not isinstance(value, str) or not value:
        raise CostCompareError(f"{field} must be a non-empty string")
    return value


def _non_negative_int(value, field):
    if not _is_int(value) or value < 0:
        raise CostCompareError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value, field):
    if not _is_int(value) or value <= 0:
        raise CostCompareError(f"{field} must be a positive integer")
    return value


def _require_object(value, field):
    if not isinstance(value, dict):
        raise CostCompareError(f"{field} must be a JSON object")
    return value


def _require_list(value, field):
    if not isinstance(value, list):
        raise CostCompareError(f"{field} must be a list")
    return value


def _require_key(mapping, key, context):
    if key not in mapping:
        raise CostCompareError(f"{context} is missing required field {key!r}")
    return mapping[key]


def _canonical_quantity(numerator, denominator):
    """Exact, JSON-friendly canonical form of a positive rational quantity."""
    factor = math.gcd(numerator, denominator)
    num, den = numerator // factor, denominator // factor
    if den == 1:
        return num
    return {"numerator": num, "denominator": den}


# ---------------------------------------------------------------------------
# Input validation and indexing
# ---------------------------------------------------------------------------


def index_review(review):
    """Map every offer in a ``bid-review`` result to its lot and price.

    Only the documented projection fields (``lot_id``, ``offer_id`` and
    ``price_cents``) are read; award, eligibility and qualification fields are
    irrelevant to cost analysis and are tolerated.
    """
    _require_object(review, "review file")
    _reject_floats(review, "review")
    lots = _require_list(review.get("lots"), "review.lots")
    if not lots:
        raise CostCompareError("review.lots must contain at least one lot")
    offers = {}
    for lot_index, lot in enumerate(lots):
        lot_context = f"review.lots[{lot_index}]"
        _require_object(lot, lot_context)
        lot_id = _non_empty_string(
            _require_key(lot, "lot_id", lot_context), f"{lot_context}.lot_id"
        )
        rows = _require_list(
            _require_key(lot, "offers", lot_context), f"{lot_context}.offers"
        )
        for offer_index, row in enumerate(rows):
            context = f"{lot_context}.offers[{offer_index}]"
            _require_object(row, context)
            offer_id = _non_empty_string(
                _require_key(row, "offer_id", context), f"{context}.offer_id"
            )
            price = _non_negative_int(
                _require_key(row, "price_cents", context),
                f"{context}.price_cents",
            )
            if offer_id in offers:
                raise CostCompareError(
                    f"review offer_id {offer_id!r} appears in more than one lot"
                )
            offers[offer_id] = {"lot_id": lot_id, "price_cents": price}
    return offers


def _validate_services(annex):
    services = _require_list(annex.get("services"), "annex.services")
    result = {}
    for index, service in enumerate(services):
        context = f"annex.services[{index}]"
        _require_object(service, context)
        service_id = _non_empty_string(
            _require_key(service, "service_id", context),
            f"{context}.service_id",
        )
        if service_id in result:
            raise CostCompareError(
                f"annex service_id {service_id!r} is declared more than once"
            )
        base_unit = _non_empty_string(
            _require_key(service, "base_unit", context), f"{context}.base_unit"
        )
        units = _require_list(
            _require_key(service, "units", context), f"{context}.units"
        )
        conversions = {base_unit: Fraction(1)}
        for unit_index, unit_row in enumerate(units):
            unit_context = f"{context}.units[{unit_index}]"
            _require_object(unit_row, unit_context)
            unit = _non_empty_string(
                _require_key(unit_row, "unit", unit_context),
                f"{unit_context}.unit",
            )
            numerator = _positive_int(
                _require_key(unit_row, "numerator", unit_context),
                f"{unit_context}.numerator",
            )
            denominator = _positive_int(
                _require_key(unit_row, "denominator", unit_context),
                f"{unit_context}.denominator",
            )
            if unit == base_unit:
                raise CostCompareError(
                    f"{unit_context}.unit {unit!r} duplicates the base unit "
                    f"of service {service_id!r}"
                )
            if unit in conversions:
                raise CostCompareError(
                    f"unit {unit!r} is declared more than once for service "
                    f"{service_id!r}"
                )
            conversions[unit] = Fraction(numerator, denominator)
        result[service_id] = {
            "service_id": service_id,
            "base_unit": base_unit,
            "conversions": conversions,
        }
    if not result:
        raise CostCompareError("annex.services must contain at least one service")
    return result


def _validate_totals(rows, id_field, amount_field, context, known_ids, line_ids):
    """Validate one subtotal list and enforce exact line coverage."""
    _require_list(rows, context)
    totals = {}
    for index, row in enumerate(rows):
        row_context = f"{context}[{index}]"
        _require_object(row, row_context)
        row_id = _non_empty_string(
            _require_key(row, id_field, row_context), f"{row_context}.{id_field}"
        )
        amount = _non_negative_int(
            _require_key(row, amount_field, row_context),
            f"{row_context}.{amount_field}",
        )
        if row_id in totals:
            raise CostCompareError(
                f"{row_context}.{id_field} {row_id!r} is listed more than once"
            )
        if row_id not in known_ids:
            raise CostCompareError(
                f"{row_context}.{id_field} {row_id!r} does not appear in any "
                "annex line of this quote"
            )
        totals[row_id] = amount
    missing = sorted(line_ids - set(totals))
    extra = sorted(set(totals) - line_ids)
    if missing or extra:
        details = []
        if missing:
            details.append("missing subtotals for " + ", ".join(missing))
        if extra:
            details.append("subtotals without lines for " + ", ".join(extra))
        raise CostCompareError(
            f"{context} must cover exactly the lines in this quote: "
            + "; ".join(details)
        )
    return totals


def validate_annex(annex, review_offers):
    """Validate the annex document, returning parsed services and quotes."""
    _require_object(annex, "annex file")
    _reject_floats(annex, "annex")
    services = _validate_services(annex)
    raw_quotes = _require_list(annex.get("quotes"), "annex.quotes")
    if not raw_quotes:
        raise CostCompareError("annex.quotes must contain at least one quote")
    quotes = {}
    for quote_index, raw_quote in enumerate(raw_quotes):
        context = f"annex.quotes[{quote_index}]"
        _require_object(raw_quote, context)
        lot_id = _non_empty_string(
            _require_key(raw_quote, "lot_id", context), f"{context}.lot_id"
        )
        offer_id = _non_empty_string(
            _require_key(raw_quote, "offer_id", context), f"{context}.offer_id"
        )
        review_offer = review_offers.get(offer_id)
        if review_offer is None or review_offer["lot_id"] != lot_id:
            raise CostCompareError(
                f"{context} references unknown lot {lot_id!r} / offer "
                f"{offer_id!r} from the review"
            )
        key = (lot_id, offer_id)
        if key in quotes:
            raise CostCompareError(
                f"annex contains more than one quote for lot {lot_id!r}, "
                f"offer {offer_id!r}"
            )

        raw_lines = _require_list(
            _require_key(raw_quote, "lines", context), f"{context}.lines"
        )
        lines = []
        line_keys = set()
        line_sites = set()
        line_services = set()
        for line_index, raw_line in enumerate(raw_lines):
            line_context = f"{context}.lines[{line_index}]"
            _require_object(raw_line, line_context)
            site_id = _non_empty_string(
                _require_key(raw_line, "site_id", line_context),
                f"{line_context}.site_id",
            )
            service_id = _non_empty_string(
                _require_key(raw_line, "service_id", line_context),
                f"{line_context}.service_id",
            )
            if service_id not in services:
                raise CostCompareError(
                    f"{line_context}.service_id {service_id!r} is not declared "
                    "in annex.services"
                )
            line_key = (site_id, service_id)
            if line_key in line_keys:
                raise CostCompareError(
                    f"{line_context}: site {site_id!r}, service {service_id!r} "
                    "is listed more than once in this quote"
                )
            quantity = _positive_int(
                _require_key(raw_line, "quantity", line_context),
                f"{line_context}.quantity",
            )
            unit = _non_empty_string(
                _require_key(raw_line, "unit", line_context),
                f"{line_context}.unit",
            )
            conversions = services[service_id]["conversions"]
            if unit not in conversions:
                raise CostCompareError(
                    f"{line_context}.unit {unit!r} is not declared for service "
                    f"{service_id!r}"
                )
            min_cents = _non_negative_int(
                _require_key(raw_line, "min_cents", line_context),
                f"{line_context}.min_cents",
            )
            max_cents = _non_negative_int(
                _require_key(raw_line, "max_cents", line_context),
                f"{line_context}.max_cents",
            )
            if min_cents > max_cents:
                raise CostCompareError(
                    f"{line_context}: min_cents {min_cents} is greater than "
                    f"max_cents {max_cents}"
                )
            line_keys.add(line_key)
            line_sites.add(site_id)
            line_services.add(service_id)
            lines.append(
                {
                    "site_id": site_id,
                    "service_id": service_id,
                    "quantity": quantity,
                    "unit": unit,
                    "min_cents": min_cents,
                    "max_cents": max_cents,
                    "base_quantity": quantity * conversions[unit],
                }
            )

        site_totals = _validate_totals(
            _require_key(raw_quote, "site_totals", context),
            "site_id",
            "total_cents",
            f"{context}.site_totals",
            line_sites,
            line_sites,
        )
        service_totals = _validate_totals(
            _require_key(raw_quote, "service_totals", context),
            "service_id",
            "total_cents",
            f"{context}.service_totals",
            line_services,
            line_services,
        )
        quotes[key] = {
            "lot_id": lot_id,
            "offer_id": offer_id,
            "lines": lines,
            "site_totals": site_totals,
            "service_totals": service_totals,
        }
    return services, quotes


def validate_basket(basket, services, quotes, review_offers):
    """Validate the comparison basket, returning a normalised copy."""
    _require_object(basket, "basket file")
    _reject_floats(basket, "basket")
    lot_id = _non_empty_string(
        _require_key(basket, "lot_id", "basket"), "basket.lot_id"
    )
    lot_offer_ids = {
        offer_id
        for offer_id, row in review_offers.items()
        if row["lot_id"] == lot_id
    }
    if not lot_offer_ids:
        raise CostCompareError(
            f"basket.lot_id {lot_id!r} does not occur in the review"
        )

    offer_ids = _require_list(
        _require_key(basket, "offer_ids", "basket"), "basket.offer_ids"
    )
    selected = []
    seen_offers = set()
    for index, offer_id in enumerate(offer_ids):
        field = f"basket.offer_ids[{index}]"
        offer_id = _non_empty_string(offer_id, field)
        if offer_id in seen_offers:
            raise CostCompareError(
                f"basket.offer_ids lists {offer_id!r} more than once"
            )
        seen_offers.add(offer_id)
        if offer_id not in lot_offer_ids:
            raise CostCompareError(
                f"basket offer {offer_id!r} is not a reviewed offer of lot "
                f"{lot_id!r}"
            )
        if (lot_id, offer_id) not in quotes:
            raise CostCompareError(
                f"basket offer {offer_id!r} has no annex quote for lot "
                f"{lot_id!r}"
            )
        selected.append(offer_id)
    if len(selected) < 2:
        raise CostCompareError("basket.offer_ids must contain at least two offers")

    raw_items = _require_list(
        _require_key(basket, "items", "basket"), "basket.items"
    )
    if not raw_items:
        raise CostCompareError("basket.items must not be empty")
    items = []
    seen_items = set()
    for index, raw_item in enumerate(raw_items):
        context = f"basket.items[{index}]"
        _require_object(raw_item, context)
        site_id = _non_empty_string(
            _require_key(raw_item, "site_id", context), f"{context}.site_id"
        )
        service_id = _non_empty_string(
            _require_key(raw_item, "service_id", context),
            f"{context}.service_id",
        )
        if service_id not in services:
            raise CostCompareError(
                f"{context}.service_id {service_id!r} is not declared in "
                "annex.services"
            )
        item_key = (site_id, service_id)
        if item_key in seen_items:
            raise CostCompareError(
                f"{context}: site {site_id!r}, service {service_id!r} is "
                "selected more than once"
            )
        seen_items.add(item_key)
        quantity = _positive_int(
            _require_key(raw_item, "quantity", context), f"{context}.quantity"
        )
        unit = _non_empty_string(
            _require_key(raw_item, "unit", context), f"{context}.unit"
        )
        conversions = services[service_id]["conversions"]
        if unit not in conversions:
            raise CostCompareError(
                f"{context}.unit {unit!r} is not declared for service "
                f"{service_id!r}"
            )
        base_quantity = quantity * conversions[unit]
        items.append(
            {
                "site_id": site_id,
                "service_id": service_id,
                "quantity": quantity,
                "unit": unit,
                "base_quantity": base_quantity,
                "normalized_quantity": _canonical_quantity(
                    base_quantity.numerator, base_quantity.denominator
                ),
            }
        )
    return {
        "lot_id": lot_id,
        "offer_ids": selected,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Bounded transportation solver (integer min-cost flow, exact arithmetic)
# ---------------------------------------------------------------------------


class _FlowNetwork:
    """Residual-capacity flow network with per-edge integer costs.

    Edge pairs are stored consecutively so the reverse edge of index ``i`` is
    ``i ^ 1``.  Edges are inserted in sorted, deterministic order so the
    shortest-path augmentations and resulting allocations are reproducible.
    """

    def __init__(self, node_count):
        self.node_count = node_count
        self.adjacency = [[] for _ in range(node_count)]
        self.edges = []  # [from_node, to_node, capacity, cost, flow]

    def add_edge(self, u, v, capacity, cost):
        index = len(self.edges)
        self.edges.append([u, v, capacity, cost, 0])
        self.edges.append([v, u, 0, -cost, 0])
        self.adjacency[u].append(index)
        self.adjacency[v].append(index + 1)
        return index

    def augment_shortest_path(self, source, sink):
        """Push flow along one shortest (SPFA) residual path.

        Returns ``(pushed, unit_cost)``; ``pushed`` is 0 when no augmenting
        path remains.  SPFA tolerates negative edge costs; successive shortest
        augmenting paths keep the residual graph free of negative-cost cycles.
        """
        distances = [None] * self.node_count
        previous_edge = [-1] * self.node_count
        in_queue = [False] * self.node_count
        distances[source] = 0
        queue = deque([source])
        in_queue[source] = True
        while queue:
            u = queue.popleft()
            in_queue[u] = False
            base = distances[u]
            for edge_index in self.adjacency[u]:
                edge = self.edges[edge_index]
                residual = edge[2] - edge[4]
                if residual <= 0:
                    continue
                v = edge[1]
                candidate = base + edge[3]
                if distances[v] is None or candidate < distances[v]:
                    distances[v] = candidate
                    previous_edge[v] = edge_index
                    if not in_queue[v]:
                        queue.append(v)
                        in_queue[v] = True
        if distances[sink] is None:
            return 0, 0
        pushed = None
        node = sink
        while node != source:
            edge = self.edges[previous_edge[node]]
            residual = edge[2] - edge[4]
            pushed = residual if pushed is None else min(pushed, residual)
            node = edge[0]
        unit_cost = 0
        node = sink
        while node != source:
            edge_index = previous_edge[node]
            edge = self.edges[edge_index]
            unit_cost += edge[3]
            edge[4] += pushed
            self.edges[edge_index ^ 1][4] -= pushed
            node = edge[0]
        return pushed, unit_cost

    def min_cost_max_flow(self, source, sink, required_flow):
        total_flow = 0
        total_cost = 0
        while total_flow < required_flow:
            pushed, unit_cost = self.augment_shortest_path(source, sink)
            if pushed == 0:
                break
            total_flow += pushed
            total_cost += pushed * unit_cost
        return total_flow, total_cost

    def reachable_from(self, source):
        seen = {source}
        queue = deque([source])
        while queue:
            u = queue.popleft()
            for edge_index in self.adjacency[u]:
                edge = self.edges[edge_index]
                if edge[2] - edge[4] <= 0:
                    continue
                v = edge[1]
                if v not in seen:
                    seen.add(v)
                    queue.append(v)
        return seen


def _build_network(site_ids, service_ids, site_supply, service_demand, lines, costs):
    """Build the lower-bound-free bipartite flow network.

    Line lower bounds are shifted into node imbalances first, so every
    remaining edge capacity is ``max_cents - min_cents``.  ``costs`` maps each
    ``(site_id, service_id)`` line key to its integer flow cost.
    """
    source = 0
    site_nodes = {site_id: index + 1 for index, site_id in enumerate(site_ids)}
    service_nodes = {
        service_id: 1 + len(site_ids) + index
        for index, service_id in enumerate(service_ids)
    }
    sink = 1 + len(site_ids) + len(service_ids)
    network = _FlowNetwork(sink + 1)
    line_edges = {}
    for site_id in site_ids:
        network.add_edge(source, site_nodes[site_id], site_supply[site_id], 0)
    for service_id in service_ids:
        network.add_edge(service_nodes[service_id], sink, service_demand[service_id], 0)
    for line in sorted(lines, key=lambda row: (row["site_id"], row["service_id"])):
        key = (line["site_id"], line["service_id"])
        capacity = line["max_cents"] - line["min_cents"]
        edge_index = network.add_edge(
            site_nodes[line["site_id"]],
            service_nodes[line["service_id"]],
            capacity,
            costs[key],
        )
        line_edges[key] = edge_index
    return network, source, sink, line_edges


def _cut_conflict(lines, site_totals, service_totals, cut_sites, cut_services):
    """Build the Hoffman-cut certificate for explicit site set S and service set T.

    The subtotal difference is ``sum(site S) - sum(service T)``; the bound is
    ``sum(upper bounds of lines in S x not-T) - sum(lower bounds of lines in
    not-S x T)``.  Every caller guarantees the difference is strictly greater
    than the bound, which makes the certificate a direct proof that the
    subtotals and line bounds cannot hold simultaneously.
    """
    site_set = set(cut_sites)
    service_set = set(cut_services)
    difference = (
        sum(site_totals[site_id] for site_id in cut_sites)
        - sum(service_totals[service_id] for service_id in cut_services)
    )
    bound = (
        sum(
            line["max_cents"]
            for line in lines
            if line["site_id"] in site_set
            and line["service_id"] not in service_set
        )
        - sum(
            line["min_cents"]
            for line in lines
            if line["site_id"] not in site_set
            and line["service_id"] in service_set
        )
    )
    return {
        "site_ids": list(cut_sites),
        "service_ids": list(cut_services),
        "subtotal_difference_cents": difference,
        "bound_cents": bound,
    }


def _boundary_conflict(site_ids, service_ids, lines, site_totals, service_totals):
    """Return a violating Hoffman cut for single-row/column bound breaches.

    The generic min-cut certificate only applies once every row and column is
    individually within its own line bounds.  The four boundary cases handled
    here each construct the witness cut (S, T) directly; the shared Hoffman
    formula then proves the breach.
    """
    by_site = {}
    by_service = {}
    for line in lines:
        by_site.setdefault(line["site_id"], []).append(line)
        by_service.setdefault(line["service_id"], []).append(line)

    all_services = list(service_ids)
    for site_id in site_ids:
        rows = by_site[site_id]
        upper = sum(line["max_cents"] for line in rows)
        lower = sum(line["min_cents"] for line in rows)
        if site_totals[site_id] > upper:
            # Row subtotal above its line upper bounds: S = {site}, T = empty.
            return _cut_conflict(
                lines, site_totals, service_totals, [site_id], []
            )
        if site_totals[site_id] < lower:
            # Row subtotal below its line lower bounds: S = all other sites,
            # T = all services.  Difference is -row subtotal, bound is -row
            # lower-bound sum.
            other_sites = [other for other in site_ids if other != site_id]
            return _cut_conflict(
                lines, site_totals, service_totals, other_sites, all_services
            )

    all_sites = list(site_ids)
    for service_id in service_ids:
        columns = by_service[service_id]
        upper = sum(line["max_cents"] for line in columns)
        lower = sum(line["min_cents"] for line in columns)
        if service_totals[service_id] > upper:
            # Column subtotal above its line upper bounds: S = all sites,
            # T = all other services.
            other_services = [
                other for other in service_ids if other != service_id
            ]
            return _cut_conflict(
                lines, site_totals, service_totals, all_sites, other_services
            )
        if service_totals[service_id] < lower:
            # Column subtotal below its line lower bounds: S = empty,
            # T = {service}.
            return _cut_conflict(
                lines, site_totals, service_totals, [], [service_id]
            )
    return None


def solve_transportation(quote, basket_keys, direction):
    """Solve one bounded transportation instance exactly.

    ``direction`` is ``"min"`` or ``"max"`` for basket-cost extrema, or
    ``"feasible"`` for a plain feasibility allocation (all line costs zero).
    Returns ``(feasible, allocations, conflict)`` where ``allocations`` maps
    each line key to an integer amount, and ``conflict`` is a Hoffman-cut
    certificate (without line detail) when no allocation exists.
    """
    lines = quote["lines"]
    site_ids = sorted(quote["site_totals"])
    service_ids = sorted(quote["service_totals"])

    site_residual = {
        site_id: quote["site_totals"][site_id]
        - sum(
            line["min_cents"] for line in lines if line["site_id"] == site_id
        )
        for site_id in site_ids
    }
    service_residual = {
        service_id: quote["service_totals"][service_id]
        - sum(
            line["min_cents"]
            for line in lines
            if line["service_id"] == service_id
        )
        for service_id in service_ids
    }

    conflict = _boundary_conflict(
        site_ids,
        service_ids,
        lines,
        quote["site_totals"],
        quote["service_totals"],
    )
    if conflict is not None:
        return False, None, conflict

    required_flow = sum(site_residual.values())
    if direction == "feasible":
        costs = {
            (line["site_id"], line["service_id"]): 0 for line in lines
        }
    else:
        sign = 1 if direction == "min" else -1
        costs = {
            (line["site_id"], line["service_id"]): (
                sign if (line["site_id"], line["service_id"]) in basket_keys else 0
            )
            for line in lines
        }

    network, source, sink, line_edges = _build_network(
        site_ids, service_ids, site_residual, service_residual, lines, costs
    )
    total_flow, _ = network.min_cost_max_flow(source, sink, required_flow)
    if total_flow != required_flow:
        reachable = network.reachable_from(source)
        reachable_sites = {
            site_id
            for index, site_id in enumerate(site_ids)
            if 1 + index in reachable
        }
        reachable_services = {
            service_id
            for index, service_id in enumerate(service_ids)
            if 1 + len(site_ids) + index in reachable
        }
        # In the lower-bound-shifted network the source side of the saturated
        # min cut is (reachable sites S, reachable services T).  Translating
        # that cut back to the original bounds yields precisely the spec's
        # Hoffman certificate: sum(S sites) - sum(T services) exceeds
        # upper-bound sum of S x not-T minus lower-bound sum of not-S x T.
        cut_sites = sorted(reachable_sites)
        cut_services = sorted(reachable_services)
        return False, None, _cut_conflict(
            lines,
            quote["site_totals"],
            quote["service_totals"],
            cut_sites,
            cut_services,
        )

    allocations = {}
    for line in lines:
        key = (line["site_id"], line["service_id"])
        edge = network.edges[line_edges[key]]
        allocations[key] = line["min_cents"] + edge[4]
    return True, allocations, None


def _conflict_lines(quote, conflict):
    """Attach the exact supporting line bounds to a Hoffman-cut conflict."""
    site_set = set(conflict["site_ids"])
    service_set = set(conflict["service_ids"])
    upper_lines = []
    lower_lines = []
    for line in quote["lines"]:
        in_site = line["site_id"] in site_set
        in_service = line["service_id"] in service_set
        if in_site and not in_service:
            upper_lines.append(
                {
                    "site_id": line["site_id"],
                    "service_id": line["service_id"],
                    "max_cents": line["max_cents"],
                }
            )
        elif not in_site and in_service:
            lower_lines.append(
                {
                    "site_id": line["site_id"],
                    "service_id": line["service_id"],
                    "min_cents": line["min_cents"],
                }
            )
    upper_lines.sort(key=lambda row: (row["site_id"], row["service_id"]))
    lower_lines.sort(key=lambda row: (row["site_id"], row["service_id"]))
    result = dict(conflict)
    result["upper_bound_lines"] = upper_lines
    result["lower_bound_lines"] = lower_lines
    if result["subtotal_difference_cents"] <= result["bound_cents"]:  # pragma: no cover
        raise RuntimeError("internal error: Hoffman cut does not prove infeasibility")
    return result



# ---------------------------------------------------------------------------
# Annex review and basket comparison
# ---------------------------------------------------------------------------


def _allocation_lines(quote, allocations):
    """Serialise one complete allocation with deterministic line order."""
    rows = []
    for line in sorted(quote["lines"], key=lambda row: (row["site_id"], row["service_id"])):
        amount = allocations[(line["site_id"], line["service_id"])]
        rows.append(
            {
                "site_id": line["site_id"],
                "service_id": line["service_id"],
                "quantity": line["quantity"],
                "unit": line["unit"],
                "min_cents": line["min_cents"],
                "max_cents": line["max_cents"],
                "amount_cents": amount,
            }
        )
    return rows


def _review_quote(quote, price_cents, basket):
    """Independently check one complete annex against its reviewed price."""
    site_grand_total = sum(quote["site_totals"].values())
    service_grand_total = sum(quote["service_totals"].values())
    total_mismatches = []
    if site_grand_total != price_cents:
        total_mismatches.append(
            {
                "subtotals": "site_totals",
                "actual_total_cents": site_grand_total,
                "expected_total_cents": price_cents,
            }
        )
    if service_grand_total != price_cents:
        total_mismatches.append(
            {
                "subtotals": "service_totals",
                "actual_total_cents": service_grand_total,
                "expected_total_cents": price_cents,
            }
        )

    line_index = {
        (line["site_id"], line["service_id"]): line for line in quote["lines"]
    }

    result = {
        "lot_id": quote["lot_id"],
        "offer_id": quote["offer_id"],
        "reviewed_price_cents": price_cents,
        "site_totals_sum_cents": site_grand_total,
        "service_totals_sum_cents": service_grand_total,
        "annex_self_consistent": False,
        "total_mismatches": total_mismatches,
        "conflict": None,
        "sample_allocation": None,
        "basket_match": None,
        "participates_in_comparison": False,
        "exclusion_reasons": [],
        "basket_min_cents": None,
        "basket_max_cents": None,
        "min_allocation": None,
        "max_allocation": None,
    }

    # Basket coverage and quantities are structural facts about the annex and
    # are reported even when the amounts cannot be balanced.
    missing = []
    quantity_mismatches = []
    for item in basket["items"]:
        key = (item["site_id"], item["service_id"])
        line = line_index.get(key)
        if line is None:
            missing.append(
                {
                    "site_id": item["site_id"],
                    "service_id": item["service_id"],
                    "reason": "no_annex_line",
                }
            )
            continue
        if line["base_quantity"] != item["base_quantity"]:
            quantity_mismatches.append(
                {
                    "site_id": item["site_id"],
                    "service_id": item["service_id"],
                    "basket": {
                        "quantity": item["quantity"],
                        "unit": item["unit"],
                        "base_quantity": item["normalized_quantity"],
                    },
                    "annex": {
                        "quantity": line["quantity"],
                        "unit": line["unit"],
                        "base_quantity": _canonical_quantity(
                            line["base_quantity"].numerator,
                            line["base_quantity"].denominator,
                        ),
                    },
                }
            )

    result["basket_match"] = {
        "matches": not missing and not quantity_mismatches,
        "missing_lines": sorted(
            missing, key=lambda row: (row["site_id"], row["service_id"])
        ),
        "quantity_mismatches": sorted(
            quantity_mismatches,
            key=lambda row: (row["site_id"], row["service_id"]),
        ),
    }
    basket_reasons = []
    if missing:
        basket_reasons.append("basket_item_missing_from_annex")
    if quantity_mismatches:
        basket_reasons.append("basket_quantity_mismatch")

    if total_mismatches:
        result["exclusion_reasons"].extend(
            ["annex_subtotal_totals_mismatch_review_price", *basket_reasons]
        )
        return result, line_index

    feasible, allocations, conflict = solve_transportation(
        quote, set(), "feasible"
    )
    if not feasible:
        result["conflict"] = _conflict_lines(quote, conflict)
        result["exclusion_reasons"].extend(
            ["annex_not_self_consistent", *basket_reasons]
        )
        return result, line_index

    result["annex_self_consistent"] = True
    result["sample_allocation"] = _allocation_lines(quote, allocations)

    if basket_reasons:
        result["exclusion_reasons"] = basket_reasons
        return result, line_index

    basket_keys = {
        (item["site_id"], item["service_id"]) for item in basket["items"]
    }
    min_feasible, min_allocations, _ = solve_transportation(
        quote, basket_keys, "min"
    )
    max_feasible, max_allocations, _ = solve_transportation(
        quote, basket_keys, "max"
    )
    if not min_feasible or not max_feasible:  # pragma: no cover - feasibility proven above
        raise RuntimeError("internal error: feasible annex lost feasibility")
    basket_min = sum(
        min_allocations[(item["site_id"], item["service_id"])]
        for item in basket["items"]
    )
    basket_max = sum(
        max_allocations[(item["site_id"], item["service_id"])]
        for item in basket["items"]
    )
    result["participates_in_comparison"] = True
    result["basket_min_cents"] = basket_min
    result["basket_max_cents"] = basket_max
    result["min_allocation"] = _allocation_lines(quote, min_allocations)
    result["max_allocation"] = _allocation_lines(quote, max_allocations)
    return result, line_index


def _pair_verdicts(offer_results):
    verdicts = []
    participants = sorted(
        (row for row in offer_results if row["participates_in_comparison"]),
        key=lambda row: row["offer_id"],
    )
    for index, left in enumerate(participants):
        for right in participants[index + 1:]:
            left_id = left["offer_id"]
            right_id = right["offer_id"]
            left_min, left_max = left["basket_min_cents"], left["basket_max_cents"]
            right_min, right_max = right["basket_min_cents"], right["basket_max_cents"]
            if left_max < right_min:
                verdict = "strictly_cheaper"
                cheaper_offer_id = left_id
                note = (
                    f"{left_id} highest possible {left_max} cents is strictly "
                    f"below {right_id} lowest possible {right_min} cents"
                )
            elif right_max < left_min:
                verdict = "strictly_cheaper"
                cheaper_offer_id = right_id
                note = (
                    f"{right_id} highest possible {right_max} cents is strictly "
                    f"below {left_id} lowest possible {left_min} cents"
                )
            elif left_min == left_max == right_min == right_max:
                verdict = "necessarily_equal"
                cheaper_offer_id = None
                note = (
                    f"both offers can only total {left_min} cents for the "
                    "selected items"
                )
            else:
                verdict = "indeterminate"
                cheaper_offer_id = None
                if left_max == right_min or right_max == left_min:
                    note = "price intervals only touch at a boundary"
                else:
                    note = "price ranges overlap; no strict price relation is guaranteed"
            verdicts.append(
                {
                    "offer_ids": [left_id, right_id],
                    "verdict": verdict,
                    "cheaper_offer_id": cheaper_offer_id,
                    "note": note,
                    "intervals_cents": {
                        left_id: {"min": left_min, "max": left_max},
                        right_id: {"min": right_min, "max": right_max},
                    },
                }
            )
    return verdicts


def compare_bid_costs(review, annex, basket):
    """Run the full annex self-consistency and basket cost comparison.

    All three arguments are parsed Python objects (use
    :func:`load_json_file` for the strict loader).  Returns a deterministic,
    JSON-serialisable result.  Raises :class:`CostCompareError` on any input
    error; business findings (annex contradictions, basket mismatches) are
    reported inside the result, never as exceptions.
    """
    review_offers = index_review(review)
    services, quotes = validate_annex(annex, review_offers)
    parsed_basket = validate_basket(basket, services, quotes, review_offers)

    offer_results = []
    for offer_id in sorted(parsed_basket["offer_ids"]):
        quote = quotes[(parsed_basket["lot_id"], offer_id)]
        price_cents = review_offers[offer_id]["price_cents"]
        result, _ = _review_quote(quote, price_cents, parsed_basket)
        offer_results.append(result)

    return {
        "schema_version": 1,
        "lot_id": parsed_basket["lot_id"],
        "basket": {
            "items": [
                {
                    "site_id": item["site_id"],
                    "service_id": item["service_id"],
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                    "base_quantity": item["normalized_quantity"],
                }
                for item in sorted(
                    parsed_basket["items"],
                    key=lambda row: (row["site_id"], row["service_id"]),
                )
            ],
            "offer_ids": sorted(parsed_basket["offer_ids"]),
        },
        "offers": offer_results,
        "pairwise_verdicts": _pair_verdicts(offer_results),
    }


def verify_allocation(quote, allocations, expected_total_cents=None):
    """Check a line-level allocation against one annex's constraints.

    ``allocations`` maps ``(site_id, service_id)`` to integer cents, or is a
    list of ``{"site_id", "service_id", "amount_cents"}`` rows.  Every line
    amount must lie within its range, row sums must equal the site subtotals,
    column sums the service subtotals, and when given the grand total must
    equal ``expected_total_cents``.  Pure integer arithmetic; the returned
    report is JSON-serialisable.
    """
    if isinstance(allocations, dict):
        amounts = dict(allocations)
    elif isinstance(allocations, list):
        amounts = {}
        for index, row in enumerate(allocations):
            if not isinstance(row, dict):
                raise CostCompareError(
                    f"allocations[{index}] must be an object"
                )
            site_id = _non_empty_string(row.get("site_id"), f"allocations[{index}].site_id")
            service_id = _non_empty_string(
                row.get("service_id"), f"allocations[{index}].service_id"
            )
            amount = _non_negative_int(
                row.get("amount_cents"), f"allocations[{index}].amount_cents"
            )
            key = (site_id, service_id)
            if key in amounts:
                raise CostCompareError(
                    f"duplicate allocation for site {site_id!r}, service "
                    f"{service_id!r}"
                )
            amounts[key] = amount
    else:
        raise CostCompareError("allocations must be a mapping or a list of rows")

    line_index = {
        (line["site_id"], line["service_id"]): line for line in quote["lines"]
    }

    def _as_mapping(rows, id_field):
        if isinstance(rows, dict):
            return dict(rows)
        return {row[id_field]: row["total_cents"] for row in rows}

    site_total_map = _as_mapping(quote["site_totals"], "site_id")
    service_total_map = _as_mapping(quote["service_totals"], "service_id")
    unknown = sorted(set(amounts) - set(line_index))
    if unknown:
        raise CostCompareError(
            "allocation covers lines absent from the annex: "
            + ", ".join(f"{site}/{service}" for site, service in unknown)
        )
    missing = sorted(set(line_index) - set(amounts))
    if missing:
        raise CostCompareError(
            "allocation is missing annex lines: "
            + ", ".join(f"{site}/{service}" for site, service in missing)
        )

    violations = []
    for key in sorted(line_index):
        line = line_index[key]
        amount = amounts[key]
        if not line["min_cents"] <= amount <= line["max_cents"]:
            violations.append(
                {
                    "site_id": line["site_id"],
                    "service_id": line["service_id"],
                    "amount_cents": amount,
                    "min_cents": line["min_cents"],
                    "max_cents": line["max_cents"],
                    "reason": "outside_line_range",
                }
            )

    site_sums = {site_id: 0 for site_id in site_total_map}
    service_sums = {service_id: 0 for service_id in service_total_map}
    for key, amount in amounts.items():
        site_sums[key[0]] += amount
        service_sums[key[1]] += amount

    subtotal_mismatches = []
    for site_id in sorted(site_total_map):
        if site_sums[site_id] != site_total_map[site_id]:
            subtotal_mismatches.append(
                {
                    "kind": "site",
                    "id": site_id,
                    "allocated_cents": site_sums[site_id],
                    "subtotal_cents": site_total_map[site_id],
                }
            )
    for service_id in sorted(service_total_map):
        if service_sums[service_id] != service_total_map[service_id]:
            subtotal_mismatches.append(
                {
                    "kind": "service",
                    "id": service_id,
                    "allocated_cents": service_sums[service_id],
                    "subtotal_cents": service_total_map[service_id],
                }
            )

    grand_total = sum(amounts.values())
    total_matches = (
        expected_total_cents is None or grand_total == expected_total_cents
    )
    return {
        "valid": not violations and not subtotal_mismatches and total_matches,
        "range_violations": violations,
        "subtotal_mismatches": subtotal_mismatches,
        "allocated_total_cents": grand_total,
        "expected_total_cents": expected_total_cents,
    }
