"""Response-time audit of property-maintenance check-ins against repair tickets.

The buyer's ticket system and the awarded supplier's check-in system share no
ticket number.  At one site the arrivals form a single response queue: when
both an earlier and a later ticket are matched, the earlier ticket must be
matched to the earlier check-in.  A check-in may therefore serve at most one
ticket and a ticket at most one check-in, and the audit reports every
relationship that can occur in *some* maximum reconciliation rather than
choosing one arbitrary pairing.

Within one ``(lot_id, site_id)`` the problem is a maximum non-crossing
matching between tickets ordered by ``(opened_at, ticket_id)`` and check-ins
ordered by ``(arrived_at, checkin_id)``.  Such a matching is a longest chain
in the two-dimensional order on candidate edges, so:

* a candidate edge lies in some maximum matching exactly when the longest
  chain ending at it plus the longest chain starting at it, minus one, equals
  the maximum chain length (``L + R - 1 == M``);
* a ticket can be left unmatched by some maximum matching exactly when it is
  reachable from an unmatched ticket through alternating paths of the
  candidate bipartite graph.

Both computations are ``O(E log N)`` / ``O(E)`` for ``E`` candidate edges, so
short search windows over tens of thousands of records stay cheap.  This
module only audits an already-archived award; it never re-runs the award and
produces no acceptance receipt or deduction.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "ResponseAuditError",
    "audit_responses",
    "load_json_file",
    "parse_json_text",
]


class ResponseAuditError(ValueError):
    """The review or log input is invalid.  No report may be produced."""


# ---------------------------------------------------------------------------
# Strict JSON loading
# ---------------------------------------------------------------------------


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ResponseAuditError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise ResponseAuditError(f"non-finite JSON number {name} is not allowed")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ResponseAuditError(f"non-finite JSON number {text} is not allowed")
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
    except ResponseAuditError as exc:
        raise ResponseAuditError(f"{source}: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise ResponseAuditError(f"{source}: invalid JSON: {exc}") from exc


def load_json_file(path):
    """Read and strictly parse one local JSON file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return parse_json_text(text, str(path))


# ---------------------------------------------------------------------------
# Field validation helpers
# ---------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value, field):
    if not isinstance(value, dict):
        raise ResponseAuditError(f"{field} must be a JSON object")
    return value


def _require_list(value, field):
    if not isinstance(value, list):
        raise ResponseAuditError(f"{field} must be a list")
    return value


def _require_key(mapping, key, context):
    if key not in mapping:
        raise ResponseAuditError(f"{context} is missing required field {key!r}")
    return mapping[key]


def _non_empty_string(value, field):
    if not isinstance(value, str) or not value:
        raise ResponseAuditError(f"{field} must be a non-empty string")
    return value


def _require_bool(value, field):
    if not isinstance(value, bool):
        raise ResponseAuditError(f"{field} must be a boolean")
    return value


def _positive_int(value, field):
    if not _is_int(value) or value <= 0:
        raise ResponseAuditError(f"{field} must be a positive integer")
    return value


def _optional_string(value, field):
    if value is None:
        return None
    return _non_empty_string(value, field)


def _parse_instant(value, field):
    """Parse one ISO-8601 timestamp that carries a UTC offset.

    Any offset is accepted; comparison is by actual instant, so the same
    instant written with different offsets is the same fact.
    """
    if not isinstance(value, str):
        raise ResponseAuditError(f"{field} must be an ISO 8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ResponseAuditError(
            f"{field} is not a valid ISO 8601 timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResponseAuditError(f"{field} requires a UTC offset: {value!r}")
    try:
        # Normalise to UTC so every later comparison is overflow-free and uses
        # one representation; offsets are otherwise irrelevant.
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise ResponseAuditError(
            f"{field} is outside the representable instant range: {value!r}"
        ) from exc


def _format_instant(value):
    return value.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Input validation and indexing
# ---------------------------------------------------------------------------


def _index_review(review):
    """Map every lot in an archived bid review to its award information.

    Only the lot and awarded-supplier identity are used.  Structural fields
    are checked for every lot, while the award consistency rule is applied
    when a lot is actually referenced by the log input: such a lot must be
    awarded, and its ``award_offer_id`` must name its one awarded offer whose
    ``vendor_id`` equals ``award_vendor_id``.  Other review fields are
    tolerated and ignored.
    """
    _require_object(review, "review file")
    lots = _require_list(_require_key(review, "lots", "review"), "review.lots")
    indexed = {}
    seen_lots = set()
    seen_offers = set()
    for lot_index, lot in enumerate(lots):
        context = f"review.lots[{lot_index}]"
        _require_object(lot, context)
        lot_id = _non_empty_string(
            _require_key(lot, "lot_id", context), f"{context}.lot_id"
        )
        if lot_id in seen_lots:
            raise ResponseAuditError(f"duplicate review lot_id {lot_id!r}")
        seen_lots.add(lot_id)
        offers = _require_list(
            _require_key(lot, "offers", context), f"{context}.offers"
        )
        awarded_offers = []
        for offer_index, offer in enumerate(offers):
            offer_context = f"{context}.offers[{offer_index}]"
            _require_object(offer, offer_context)
            offer_id = _non_empty_string(
                _require_key(offer, "offer_id", offer_context),
                f"{offer_context}.offer_id",
            )
            if offer_id in seen_offers:
                raise ResponseAuditError(
                    f"duplicate review offer_id {offer_id!r}"
                )
            seen_offers.add(offer_id)
            vendor_id = _non_empty_string(
                _require_key(offer, "vendor_id", offer_context),
                f"{offer_context}.vendor_id",
            )
            if _require_bool(
                _require_key(offer, "awarded", offer_context),
                f"{offer_context}.awarded",
            ):
                awarded_offers.append((offer_id, vendor_id))
        indexed[lot_id] = {
            "context": context,
            "awarded": _require_bool(
                _require_key(lot, "awarded", context), f"{context}.awarded"
            ),
            "award_offer_id": _optional_string(
                _require_key(lot, "award_offer_id", context),
                f"{context}.award_offer_id",
            ),
            "award_vendor_id": _optional_string(
                _require_key(lot, "award_vendor_id", context),
                f"{context}.award_vendor_id",
            ),
            "awarded_offers": awarded_offers,
        }
    return indexed


def _awarded_vendor(lot_id, lot):
    """Return the lot's awarded vendor, or raise naming the inconsistency."""
    context = lot["context"]
    if not lot["awarded"]:
        raise ResponseAuditError(
            f"lot {lot_id!r} is referenced by the log input but was not "
            "awarded in the review"
        )
    awarded_offers = lot["awarded_offers"]
    if len(awarded_offers) != 1:
        raise ResponseAuditError(
            f"{context} is awarded but has {len(awarded_offers)} offers marked "
            "awarded; exactly one is required"
        )
    offer_id, vendor_id = awarded_offers[0]
    if lot["award_offer_id"] != offer_id:
        raise ResponseAuditError(
            f"{context}.award_offer_id {lot['award_offer_id']!r} does not name "
            f"its sole awarded offer {offer_id!r}"
        )
    if lot["award_vendor_id"] != vendor_id:
        raise ResponseAuditError(
            f"{context}.award_vendor_id {lot['award_vendor_id']!r} does not "
            f"match the awarded offer's vendor {vendor_id!r}"
        )
    return vendor_id


def _index_logs(logs, review_lots):
    """Validate the log export and group records by configured site.

    ``review_lots`` is the index built by :func:`_index_review`; a referenced
    lot must resolve to exactly one consistent award.
    """
    _require_object(logs, "log file")
    closed_through = _parse_instant(
        _require_key(logs, "closed_through", "logs"), "logs.closed_through"
    )
    raw_sites = _require_list(
        _require_key(logs, "sites", "logs"), "logs.sites"
    )
    raw_tickets = _require_list(
        _require_key(logs, "tickets", "logs"), "logs.tickets"
    )
    raw_checkins = _require_list(
        _require_key(logs, "checkins", "logs"), "logs.checkins"
    )

    sites = {}
    for index, site in enumerate(raw_sites):
        context = f"logs.sites[{index}]"
        _require_object(site, context)
        lot_id = _non_empty_string(
            _require_key(site, "lot_id", context), f"{context}.lot_id"
        )
        site_id = _non_empty_string(
            _require_key(site, "site_id", context), f"{context}.site_id"
        )
        sla = _positive_int(
            _require_key(site, "sla_minutes", context),
            f"{context}.sla_minutes",
        )
        search = _positive_int(
            _require_key(site, "search_minutes", context),
            f"{context}.search_minutes",
        )
        if sla > search:
            raise ResponseAuditError(
                f"{context}.sla_minutes ({sla}) must not exceed "
                f"search_minutes ({search})"
            )
        key = (lot_id, site_id)
        if key in sites:
            raise ResponseAuditError(
                f"duplicate site ({key[0]!r}, {key[1]!r}) in logs.sites"
            )
        if lot_id not in review_lots:
            raise ResponseAuditError(
                f"{context} references unknown review lot {lot_id!r}"
            )
        _awarded_vendor(lot_id, review_lots[lot_id])
        sites[key] = {
            "sla_minutes": sla,
            "search_minutes": search,
            "tickets": [],
            "checkins": [],
        }

    seen_tickets = set()
    for index, ticket in enumerate(raw_tickets):
        context = f"logs.tickets[{index}]"
        _require_object(ticket, context)
        ticket_id = _non_empty_string(
            _require_key(ticket, "ticket_id", context), f"{context}.ticket_id"
        )
        if ticket_id in seen_tickets:
            raise ResponseAuditError(f"duplicate ticket_id {ticket_id!r}")
        seen_tickets.add(ticket_id)
        lot_id = _non_empty_string(
            _require_key(ticket, "lot_id", context), f"{context}.lot_id"
        )
        site_id = _non_empty_string(
            _require_key(ticket, "site_id", context), f"{context}.site_id"
        )
        key = (lot_id, site_id)
        if key not in sites:
            raise ResponseAuditError(
                f"{context} references unknown site ({lot_id!r}, {site_id!r})"
            )
        opened_at = _parse_instant(
            _require_key(ticket, "opened_at", context), f"{context}.opened_at"
        )
        try:
            window_end = opened_at + timedelta(
                minutes=sites[key]["search_minutes"]
            )
        except OverflowError as exc:
            raise ResponseAuditError(
                f"{context}.opened_at search window extends beyond the "
                "representable instant range"
            ) from exc
        if window_end > closed_through:
            raise ResponseAuditError(
                f"{context}.opened_at search window ends at "
                f"{_format_instant(window_end)}, after logs.closed_through "
                f"{_format_instant(closed_through)}; the export is incomplete "
                "for this ticket"
            )
        sites[key]["tickets"].append((opened_at, ticket_id))

    seen_checkins = set()
    for index, checkin in enumerate(raw_checkins):
        context = f"logs.checkins[{index}]"
        _require_object(checkin, context)
        checkin_id = _non_empty_string(
            _require_key(checkin, "checkin_id", context), f"{context}.checkin_id"
        )
        if checkin_id in seen_checkins:
            raise ResponseAuditError(f"duplicate checkin_id {checkin_id!r}")
        seen_checkins.add(checkin_id)
        lot_id = _non_empty_string(
            _require_key(checkin, "lot_id", context), f"{context}.lot_id"
        )
        site_id = _non_empty_string(
            _require_key(checkin, "site_id", context), f"{context}.site_id"
        )
        key = (lot_id, site_id)
        if key not in sites:
            raise ResponseAuditError(
                f"{context} references unknown site ({lot_id!r}, {site_id!r})"
            )
        vendor_id = _non_empty_string(
            _require_key(checkin, "vendor_id", context), f"{context}.vendor_id"
        )
        awarded_vendor = _awarded_vendor(lot_id, review_lots[lot_id])
        if vendor_id != awarded_vendor:
            raise ResponseAuditError(
                f"{context}.vendor_id {vendor_id!r} is not the awarded vendor "
                f"{awarded_vendor!r} for lot {lot_id!r}"
            )
        arrived_at = _parse_instant(
            _require_key(checkin, "arrived_at", context),
            f"{context}.arrived_at",
        )
        if arrived_at > closed_through:
            raise ResponseAuditError(
                f"{context}.arrived_at {_format_instant(arrived_at)} is after "
                f"logs.closed_through {_format_instant(closed_through)}; the "
                "export is incomplete"
            )
        sites[key]["checkins"].append((arrived_at, checkin_id))

    return closed_through, sites


# ---------------------------------------------------------------------------
# Maximum non-crossing reconciliation for one site
# ---------------------------------------------------------------------------


def _solve_site(tickets, checkins, sla, search):
    """Compute maximum pairs and every ticket relationship that is provable.

    ``tickets`` is ``[(opened_at, ticket_id)]`` sorted by business order and
    ``checkins`` is ``[(arrived_at, checkin_id)]`` sorted the same way.
    Candidate edges link a ticket to every check-in in the inclusive window
    ``[opened_at, opened_at + search]``.  A non-crossing matching is exactly a
    chain of edges increasing in both ticket and check-in order, so the
    maximum pair count is a longest chain and the edges of ``max_pairs``
    chains are those with ``left + right - 1 == max_pairs``.
    """
    n = len(tickets)
    m = len(checkins)
    # Work in integer microseconds so the inner loops avoid ``timedelta``
    # comparisons; the mapping is exact for every representable timestamp.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    ticket_times = [
        (opened_at - epoch) // timedelta(microseconds=1) for opened_at, _ in tickets
    ]
    checkin_times = [
        (arrived_at - epoch) // timedelta(microseconds=1) for arrived_at, _ in checkins
    ]
    window_us = search * 60 * 1_000_000
    sla_us = sla * 60 * 1_000_000

    # Candidate edges, generated ticket by ticket with ascending check-in
    # index; ``starts[i]:ends[i]`` slices the flat array for ticket ``i``.
    edge_checkins = []
    starts = [0] * n
    ends = [0] * n
    for i in range(n):
        opened_at = ticket_times[i]
        starts[i] = len(edge_checkins)
        lo = bisect_left(checkin_times, opened_at)
        hi = bisect_right(checkin_times, opened_at + window_us)
        edge_checkins.extend(range(lo, hi))
        ends[i] = len(edge_checkins)
    edge_count = len(edge_checkins)

    # Longest chain ending at each edge.  The Fenwick tree is indexed by
    # check-in position and answers "best edge with a strictly earlier
    # check-in"; all edges of one ticket are queried before any is inserted so
    # two edges of the same ticket can never chain.
    left_length = [0] * edge_count
    predecessor = [-1] * edge_count
    bit_length = [0] * (m + 1)
    bit_edge = [-1] * (m + 1)
    for i in range(n):
        for p in range(starts[i], ends[i]):
            j = edge_checkins[p]
            best = 0
            best_edge = -1
            x = j
            while x > 0:
                if bit_length[x] > best:
                    best = bit_length[x]
                    best_edge = bit_edge[x]
                x -= x & -x
            left_length[p] = best + 1
            predecessor[p] = best_edge
        for p in range(starts[i], ends[i]):
            j = edge_checkins[p]
            value = left_length[p]
            x = j + 1
            while x <= m:
                if value > bit_length[x]:
                    bit_length[x] = value
                    bit_edge[x] = p
                x += x & -x

    max_pairs = max(left_length, default=0)

    # A witness maximum non-crossing matching, used for alternating
    # reachability below.
    match_ticket = [-1] * n
    match_checkin = [-1] * m
    if max_pairs:
        edge = max(range(edge_count), key=lambda p: left_length[p])
        while edge != -1:
            i = bisect_right(starts, edge) - 1
            j = edge_checkins[edge]
            match_ticket[i] = j
            match_checkin[j] = i
            edge = predecessor[edge]

    # Longest chain starting at each edge, scanning tickets backwards.  The
    # Fenwick tree is indexed by reversed check-in position and answers "best
    # edge with a strictly later check-in".
    right_length = [0] * edge_count
    bit_length = [0] * (m + 1)
    for i in range(n - 1, -1, -1):
        for p in range(starts[i], ends[i]):
            j = edge_checkins[p]
            reversed_j = m - 1 - j
            best = 0
            x = reversed_j
            while x > 0:
                if bit_length[x] > best:
                    best = bit_length[x]
                x -= x & -x
            right_length[p] = best + 1
        for p in range(starts[i], ends[i]):
            j = edge_checkins[p]
            value = right_length[p]
            x = m - j
            while x <= m:
                if value > bit_length[x]:
                    bit_length[x] = value
                x += x & -x

    # Alternating reachability from tickets unmatched in the witness: a
    # ticket reachable this way can be unmatched in some maximum matching.
    visited_ticket = [False] * n
    visited_checkin = [False] * m
    stack = []
    for i in range(n):
        if match_ticket[i] == -1:
            visited_ticket[i] = True
            stack.append(i)
    while stack:
        i = stack.pop()
        matched_j = match_ticket[i]
        for p in range(starts[i], ends[i]):
            j = edge_checkins[p]
            if j == matched_j or visited_checkin[j]:
                continue
            visited_checkin[j] = True
            other = match_checkin[j]
            if other != -1 and not visited_ticket[other]:
                visited_ticket[other] = True
                stack.append(other)

    rows = []
    for i, (opened_at, ticket_id) in enumerate(tickets):
        partner_ids = []
        on_time_possible = False
        late_possible = False
        for p in range(starts[i], ends[i]):
            if left_length[p] + right_length[p] - 1 != max_pairs:
                continue
            j = edge_checkins[p]
            partner_ids.append(checkins[j][1])
            if checkin_times[j] - ticket_times[i] <= sla_us:
                on_time_possible = True
            else:
                late_possible = True
        partner_ids.sort()
        can_be_unmatched = visited_ticket[i]
        possible_verdicts = []
        if on_time_possible:
            possible_verdicts.append("on_time")
        if late_possible:
            possible_verdicts.append("late")
        if can_be_unmatched:
            possible_verdicts.append("no_checkin")
        confirmed_checkin_id = (
            partner_ids[0]
            if len(partner_ids) == 1 and not can_be_unmatched
            else None
        )
        verdict = (
            possible_verdicts[0]
            if len(possible_verdicts) == 1
            else "needs_review"
        )
        rows.append(
            {
                "ticket_id": ticket_id,
                "opened_at": _format_instant(opened_at),
                "possible_checkin_ids": partner_ids,
                "can_be_unmatched": can_be_unmatched,
                "confirmed_checkin_id": confirmed_checkin_id,
                "verdict": verdict,
                "possible_verdicts": possible_verdicts,
            }
        )
    return max_pairs, rows


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def audit_responses(review, logs):
    """Audit response times against an archived award.

    ``review`` is a parsed ``bid-review`` result for one package and ``logs``
    is the parsed check-in/ticket export.  Returns a JSON-serialisable report
    without modifying either argument.  Raises :class:`ResponseAuditError`
    for any invalid input, naming the offending record or field.
    """
    review_lots = _index_review(review)
    awarded_vendors = {}
    closed_through, sites = _index_logs(logs, review_lots)

    ordered_keys = sorted(sites)
    report_lots = {}
    total_max_pairs = 0
    total_tickets = 0
    total_checkins = 0
    verdict_counts = {"on_time": 0, "late": 0, "no_checkin": 0, "needs_review": 0}

    for key in ordered_keys:
        lot_id, site_id = key
        config = sites[key]
        if lot_id not in awarded_vendors:
            awarded_vendors[lot_id] = _awarded_vendor(lot_id, review_lots[lot_id])
        tickets = sorted(config["tickets"])
        checkins = sorted(config["checkins"])
        max_pairs, rows = _solve_site(
            tickets, checkins, config["sla_minutes"], config["search_minutes"]
        )
        total_max_pairs += max_pairs
        total_tickets += len(tickets)
        total_checkins += len(checkins)
        for row in rows:
            verdict_counts[row["verdict"]] += 1
        report_lots.setdefault(lot_id, []).append(
            {
                "site_id": site_id,
                "sla_minutes": config["sla_minutes"],
                "search_minutes": config["search_minutes"],
                "tickets_total": len(tickets),
                "checkins_total": len(checkins),
                "max_pairs": max_pairs,
                "tickets": rows,
            }
        )

    lots = [
        {
            "lot_id": lot_id,
            "vendor_id": awarded_vendors[lot_id],
            "sites": report_lots[lot_id],
        }
        for lot_id in sorted(report_lots)
    ]
    return {
        "closed_through": _format_instant(closed_through),
        "lots": lots,
        "totals": {
            "lots": len(lots),
            "sites": len(ordered_keys),
            "tickets": total_tickets,
            "checkins": total_checkins,
            "max_pairs": total_max_pairs,
            "verdicts": verdict_counts,
        },
    }
