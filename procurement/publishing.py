"""Paged publication and independent reception of procurement mirrors.

The procurement centre holds a complete *release* object built from
``bid-review`` package results.  An information station holds a *mirror*:
its current supplier directory and award announcements.  This module turns
the difference between a station's base mirror and the release target into a
bundle of small, independently applicable pages, and lets a receiver decide
from one page plus its current mirror alone whether that page is the next
valid step.

A page is an all-or-nothing transaction.  Intermediate states inside a page
are never inspected, but the state at every page boundary must satisfy every
mirror invariant (known-vendor references and unique public references).
The planner therefore models the row operations as a precedence graph:

* an inserted supplier must be published before awards that reference it;
* an award that stops referencing a supplier must disappear before that
  supplier can be deleted;
* a public reference may only be acquired by its new holder after its old
  holder released it.

Edges that form a cycle force the participating operations into one page
(strongly connected component).  Everything else can be spread across pages
in any topological order, packed greedily up to the receiver's per-page
operation limit.  A component larger than that limit makes the whole
publication impossible; no partial bundle is ever produced.

Python 3.11+ standard library only.
"""

from __future__ import annotations

import copy
import hashlib
import heapq
import json
import math
from pathlib import Path

SCHEMA_VERSION = 1
TABLE_SUPPLIERS = "suppliers"
TABLE_AWARDS = "awards"
ACTION_UPSERT = "upsert"
ACTION_DELETE = "delete"

_MIRROR_FIELDS = frozenset(
    {"schema_version", "stream_id", "position", "suppliers", "awards"}
)
_RELEASE_FIELDS = frozenset(
    {"schema_version", "stream_id", "suppliers", "packages"}
)
_SUPPLIER_FIELDS = frozenset({"vendor_id", "public_ref"})
_AWARD_FIELDS = frozenset(
    {"package_id", "lot_id", "offer_id", "vendor_id", "price_cents"}
)
_PACKAGE_FIELDS = frozenset({"package_id", "result"})
_PAGE_FIELDS = frozenset(
    {
        "schema_version",
        "stream_id",
        "from_position",
        "before_sha256",
        "after_sha256",
        "operations",
    }
)
_BUNDLE_FIELDS = frozenset({"schema_version", "stream_id", "pages"})
_OPERATION_FIELDS = frozenset({"table", "action", "key", "row"})
_LOT_FIELDS = frozenset(
    {"lot_id", "offers", "award_offer_id", "award_vendor_id"}
)
_OFFER_FIELDS = frozenset(
    {"offer_id", "vendor_id", "price_cents", "awarded"}
)


class PublishError(ValueError):
    """An input document or page is invalid; no output may be produced."""


# ---------------------------------------------------------------------------
# Strict JSON loading
# ---------------------------------------------------------------------------


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublishError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise PublishError(f"non-finite JSON number {name} is not allowed")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise PublishError(f"non-finite JSON number {text} is not allowed")
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
    except PublishError as exc:
        raise PublishError(f"{source}: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise PublishError(f"{source}: invalid JSON: {exc}") from exc


def load_json_file(path):
    """Read and strictly parse one JSON file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishError(f"cannot read {path}: {exc}") from exc
    return parse_json_text(text, str(path))


# ---------------------------------------------------------------------------
# Scalar validation
# ---------------------------------------------------------------------------


def _reject_floats(value, field):
    """No document field accepts fractional or non-finite JSON numbers."""
    if isinstance(value, float):
        raise PublishError(f"{field} must be an integer, got a fractional number")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, f"{field}[{index}]")


def _non_empty_string(value, field):
    if not isinstance(value, str) or not value:
        raise PublishError(f"{field} must be a non-empty string")
    return value


def _non_negative_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublishError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PublishError(f"{field} must be a positive integer")
    return value


def _exact_fields(mapping, expected, context):
    if not isinstance(mapping, dict):
        raise PublishError(f"{context} must be an object")
    missing = sorted(expected - set(mapping))
    if missing:
        raise PublishError(
            f"{context} is missing required field(s) {', '.join(missing)}"
        )
    extra = sorted(set(mapping) - expected)
    if extra:
        raise PublishError(
            f"{context} has unknown field(s) {', '.join(extra)}"
        )


def _schema_one(value, context):
    if isinstance(value, bool) or not isinstance(value, int) or value != SCHEMA_VERSION:
        raise PublishError(f"{context} schema_version must be the integer 1")


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------


def _validate_supplier_row(row, context):
    _exact_fields(row, _SUPPLIER_FIELDS, context)
    _non_empty_string(row["vendor_id"], f"{context}.vendor_id")
    _non_empty_string(row["public_ref"], f"{context}.public_ref")
    return {"vendor_id": row["vendor_id"], "public_ref": row["public_ref"]}


def _validate_award_row(row, context):
    _exact_fields(row, _AWARD_FIELDS, context)
    _non_empty_string(row["package_id"], f"{context}.package_id")
    _non_empty_string(row["lot_id"], f"{context}.lot_id")
    _non_empty_string(row["offer_id"], f"{context}.offer_id")
    _non_empty_string(row["vendor_id"], f"{context}.vendor_id")
    _non_negative_int(row["price_cents"], f"{context}.price_cents")
    return {
        "package_id": row["package_id"],
        "lot_id": row["lot_id"],
        "offer_id": row["offer_id"],
        "vendor_id": row["vendor_id"],
        "price_cents": row["price_cents"],
    }


def _validate_directory(suppliers, context, known_vendor_ids=None):
    if not isinstance(suppliers, list):
        raise PublishError(f"{context} must be a list")
    rows = {}
    public_refs = {}
    for index, row in enumerate(suppliers):
        validated = _validate_supplier_row(row, f"{context}[{index}]")
        vendor_id = validated["vendor_id"]
        if vendor_id in rows:
            raise PublishError(
                f"{context}[{index}] repeats supplier vendor_id {vendor_id!r}"
            )
        public_ref = validated["public_ref"]
        if public_ref in public_refs:
            raise PublishError(
                f"{context}[{index}] reuses public_ref {public_ref!r}, already "
                f"held by vendor {public_refs[public_ref]!r}"
            )
        rows[vendor_id] = validated
        public_refs[public_ref] = vendor_id
    if known_vendor_ids is not None:
        unknown = sorted(set(known_vendor_ids) - set(rows))
        if unknown:
            raise PublishError(
                "awards reference supplier vendor_id(s) absent from "
                f"{context}: {', '.join(map(repr, unknown))}"
            )
    return rows


def _validate_awards(awards, context, supplier_ids):
    if not isinstance(awards, list):
        raise PublishError(f"{context} must be a list")
    rows = {}
    for index, row in enumerate(awards):
        validated = _validate_award_row(row, f"{context}[{index}]")
        key = (validated["package_id"], validated["lot_id"])
        if key in rows:
            raise PublishError(
                f"{context}[{index}] repeats award key "
                f"[{validated['package_id']!r}, {validated['lot_id']!r}]"
            )
        if validated["vendor_id"] not in supplier_ids:
            raise PublishError(
                f"{context}[{index}] references unknown supplier "
                f"vendor_id {validated['vendor_id']!r}"
            )
        rows[key] = validated
    return rows


# ---------------------------------------------------------------------------
# Mirror and release validation
# ---------------------------------------------------------------------------


def validate_mirror(mirror):
    """Validate a complete mirror object and return a normalised copy.

    The normalised copy keeps ``suppliers`` and ``awards`` as dictionaries
    keyed by their stable keys; use :func:`mirror_to_payload` when emitting
    the JSON form.
    """
    if not isinstance(mirror, dict):
        raise PublishError("mirror must be a JSON object")
    _reject_floats(mirror, "mirror")
    _exact_fields(mirror, _MIRROR_FIELDS, "mirror")
    _schema_one(mirror["schema_version"], "mirror")
    stream_id = _non_empty_string(mirror["stream_id"], "mirror.stream_id")
    _non_negative_int(mirror["position"], "mirror.position")
    suppliers = _validate_directory(mirror["suppliers"], "mirror.suppliers")
    awards = _validate_awards(
        mirror["awards"], "mirror.awards", set(suppliers)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "stream_id": stream_id,
        "position": mirror["position"],
        "suppliers": suppliers,
        "awards": awards,
    }


def mirror_to_payload(mirror):
    """Render a normalised keyed mirror as its stable JSON object form."""
    return {
        "schema_version": mirror["schema_version"],
        "stream_id": mirror["stream_id"],
        "position": mirror["position"],
        "suppliers": [
            mirror["suppliers"][key] for key in sorted(mirror["suppliers"])
        ],
        "awards": [
            mirror["awards"][key] for key in sorted(mirror["awards"])
        ],
    }


def _validate_release_suppliers(suppliers):
    if not isinstance(suppliers, list):
        raise PublishError("release.suppliers must be a list")
    rows = {}
    public_refs = {}
    for index, row in enumerate(suppliers):
        validated = _validate_supplier_row(row, f"release.suppliers[{index}]")
        vendor_id = validated["vendor_id"]
        if vendor_id in rows:
            raise PublishError(
                f"release.suppliers[{index}] repeats vendor_id {vendor_id!r}"
            )
        public_ref = validated["public_ref"]
        if public_ref in public_refs:
            raise PublishError(
                f"release.suppliers[{index}] reuses public_ref {public_ref!r}, "
                f"already held by vendor {public_refs[public_ref]!r}"
            )
        rows[vendor_id] = validated
        public_refs[public_ref] = vendor_id
    return rows


def _validate_lot(lot, context, supplier_ids, package_id):
    if not isinstance(lot, dict):
        raise PublishError(f"{context} must be an object")
    missing = sorted(_LOT_FIELDS - set(lot))
    if missing:
        raise PublishError(
            f"{context} is missing required field(s) {', '.join(missing)}"
        )
    lot_id = _non_empty_string(lot["lot_id"], f"{context}.lot_id")
    offers = lot["offers"]
    if not isinstance(offers, list):
        raise PublishError(f"{context}.offers must be a list")
    if not offers:
        raise PublishError(f"{context}.offers must not be empty")

    award_offer_id = lot["award_offer_id"]
    award_vendor_id = lot["award_vendor_id"]
    if award_offer_id is None or award_vendor_id is None:
        if award_offer_id is not None or award_vendor_id is not None:
            raise PublishError(
                f"{context} award_offer_id and award_vendor_id must both be "
                "null for an unawarded lot"
            )
        awarded_lot = False
    else:
        _non_empty_string(
            award_offer_id, f"{context}.award_offer_id"
        )
        _non_empty_string(
            award_vendor_id, f"{context}.award_vendor_id"
        )
        awarded_lot = True

    seen_offers = set()
    awarded_offer = None
    for index, offer in enumerate(offers):
        offer_context = f"{context}.offers[{index}]"
        if not isinstance(offer, dict):
            raise PublishError(f"{offer_context} must be an object")
        missing = sorted(_OFFER_FIELDS - set(offer))
        if missing:
            raise PublishError(
                f"{offer_context} is missing required field(s) "
                + ", ".join(missing)
            )
        offer_id = _non_empty_string(
            offer["offer_id"], f"{offer_context}.offer_id"
        )
        if offer_id in seen_offers:
            raise PublishError(
                f"{offer_context} repeats offer_id {offer_id!r} in lot "
                f"{lot_id!r}"
            )
        seen_offers.add(offer_id)
        vendor_id = _non_empty_string(
            offer["vendor_id"], f"{offer_context}.vendor_id"
        )
        if vendor_id not in supplier_ids:
            raise PublishError(
                f"{offer_context} references unknown supplier {vendor_id!r}"
            )
        price = _non_negative_int(
            offer["price_cents"], f"{offer_context}.price_cents"
        )
        if not isinstance(offer["awarded"], bool):
            raise PublishError(
                f"{offer_context}.awarded must be a boolean"
            )
        if offer["awarded"]:
            if awarded_offer is not None:
                raise PublishError(
                    f"{context} has more than one awarded offer "
                    f"({awarded_offer['offer_id']!r} and {offer_id!r})"
                )
            awarded_offer = {
                "offer_id": offer_id,
                "vendor_id": vendor_id,
                "price_cents": price,
            }

    if awarded_lot:
        if awarded_offer is None:
            raise PublishError(
                f"{context} names award_offer_id {award_offer_id!r} but no "
                "offer carries awarded=true"
            )
        if awarded_offer["offer_id"] != award_offer_id:
            raise PublishError(
                f"{context} award_offer_id {award_offer_id!r} does not match "
                f"the awarded offer {awarded_offer['offer_id']!r}"
            )
        if awarded_offer["vendor_id"] != award_vendor_id:
            raise PublishError(
                f"{context} award_vendor_id {award_vendor_id!r} does not match "
                f"the awarded offer vendor {awarded_offer['vendor_id']!r}"
            )
        return {
            "package_id": package_id,
            "lot_id": lot_id,
            "offer_id": awarded_offer["offer_id"],
            "vendor_id": awarded_offer["vendor_id"],
            "price_cents": awarded_offer["price_cents"],
        }
    if awarded_offer is not None:
        raise PublishError(
            f"{context} has an offer with awarded=true but both award IDs "
            "are null"
        )
    return None


def validate_release(release):
    """Validate a release object and project it to target mirror rows."""
    if not isinstance(release, dict):
        raise PublishError("release must be a JSON object")
    _reject_floats(release, "release")
    _exact_fields(release, _RELEASE_FIELDS, "release")
    _schema_one(release["schema_version"], "release")
    stream_id = _non_empty_string(release["stream_id"], "release.stream_id")
    suppliers = _validate_release_suppliers(release["suppliers"])

    packages = release["packages"]
    if not isinstance(packages, list):
        raise PublishError("release.packages must be a list")
    seen_packages = set()
    target_awards = {}
    for index, package in enumerate(packages):
        package_context = f"release.packages[{index}]"
        _exact_fields(package, _PACKAGE_FIELDS, package_context)
        package_id = _non_empty_string(
            package["package_id"], f"{package_context}.package_id"
        )
        if package_id in seen_packages:
            raise PublishError(
                f"{package_context} repeats package_id {package_id!r}"
            )
        seen_packages.add(package_id)
        result = package["result"]
        if not isinstance(result, dict):
            raise PublishError(f"{package_context}.result must be an object")
        if "lots" not in result:
            raise PublishError(
                f"{package_context}.result is missing lots"
            )
        lots = result["lots"]
        if not isinstance(lots, list):
            raise PublishError(f"{package_context}.result.lots must be a list")
        seen_lots = set()
        for lot_index, lot in enumerate(lots):
            lot_context = f"{package_context}.result.lots[{lot_index}]"
            projected = _validate_lot(
                lot, lot_context, set(suppliers), package_id
            )
            # _validate_lot already checked lot_id is a non-empty string.
            lot_id = lot["lot_id"]
            if lot_id in seen_lots:
                raise PublishError(
                    f"{lot_context} repeats lot_id {lot_id!r} "
                    f"in package {package_id!r}"
                )
            seen_lots.add(lot_id)
            if projected is None:
                continue
            key = (package_id, projected["lot_id"])
            if key in target_awards:  # pragma: no cover - guarded per package
                raise PublishError(
                    f"award key {key!r} is projected more than once"
                )
            target_awards[key] = projected

    return {
        "stream_id": stream_id,
        "suppliers": suppliers,
        "awards": target_awards,
    }


# ---------------------------------------------------------------------------
# Canonical mirror hashing
# ---------------------------------------------------------------------------


def canonical_mirror_bytes(mirror):
    """Serialise a mirror exactly as required for its position-bound hash."""
    suppliers = mirror["suppliers"]
    awards = mirror["awards"]
    if isinstance(suppliers, dict):
        suppliers = [suppliers[key] for key in sorted(suppliers)]
    else:
        suppliers = sorted(suppliers, key=lambda row: row["vendor_id"])
    if isinstance(awards, dict):
        awards = [awards[key] for key in sorted(awards)]
    else:
        awards = sorted(
            awards, key=lambda row: (row["package_id"], row["lot_id"])
        )
    ordered = {
        "schema_version": mirror["schema_version"],
        "stream_id": mirror["stream_id"],
        "position": mirror["position"],
        "suppliers": suppliers,
        "awards": awards,
    }
    return json.dumps(
        ordered,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def mirror_sha256(mirror):
    """Lower-case hex SHA-256 over the canonical serialisation."""
    return hashlib.sha256(canonical_mirror_bytes(mirror)).hexdigest()


# ---------------------------------------------------------------------------
# Row diff
# ---------------------------------------------------------------------------


def _row_operation(table, action, key, row=None):
    operation = {"table": table, "action": action, "key": key}
    if row is not None:
        operation["row"] = row
    return operation


def _diff_table(base_rows, target_rows, table, key_to_json):
    operations = {}
    for key in sorted(set(base_rows) | set(target_rows)):
        if key not in base_rows:
            operations[key] = _row_operation(
                table, ACTION_UPSERT, key_to_json(key), target_rows[key]
            )
        elif key not in target_rows:
            operations[key] = _row_operation(
                table, ACTION_DELETE, key_to_json(key)
            )
        elif base_rows[key] != target_rows[key]:
            operations[key] = _row_operation(
                table, ACTION_UPSERT, key_to_json(key), target_rows[key]
            )
    return operations


def _build_operations(base, target):
    supplier_ops = _diff_table(
        base["suppliers"],
        target["suppliers"],
        TABLE_SUPPLIERS,
        lambda key: key,
    )
    award_ops = _diff_table(
        base["awards"],
        target["awards"],
        TABLE_AWARDS,
        lambda key: [key[0], key[1]],
    )
    return supplier_ops, award_ops


# ---------------------------------------------------------------------------
# Precedence graph and page planning
# ---------------------------------------------------------------------------


def _node_order(supplier_ops, award_ops):
    nodes = [(0, key) for key in sorted(supplier_ops)]
    nodes.extend((1, key) for key in sorted(award_ops))
    index = {node: i for i, node in enumerate(nodes)}
    return nodes, index


def _build_precedence_graph(base, target, supplier_ops, award_ops):
    """Return adjacency as ``{node_index: set(node_index)}``.

    Every edge means "the source operation must land on an earlier or the
    same page as the target".  See the module docstring for the three rules.
    """
    nodes, index = _node_order(supplier_ops, award_ops)
    adjacency = {i: set() for i in range(len(nodes))}

    def edge(source, target_node):
        adjacency[index[source]].add(index[target_node])

    # Reference integrity around supplier insertions and deletions.
    for vendor_id, operation in supplier_ops.items():
        node = (0, vendor_id)
        if operation["action"] == ACTION_UPSERT:
            if vendor_id not in base["suppliers"]:
                # Inserted supplier precedes every changed award that ends
                # on it (awards that already reference it need no edge).
                for key in award_ops:
                    target_row = target["awards"].get(key)
                    if (
                        target_row is not None
                        and target_row["vendor_id"] == vendor_id
                    ):
                        edge(node, (1, key))
        else:
            # Deleted supplier follows every changed award that currently
            # names it in the base mirror.
            for key in award_ops:
                existing = base["awards"].get(key)
                if (
                    existing is not None
                    and existing["vendor_id"] == vendor_id
                ):
                    edge((1, key), node)

    # Public reference handovers: release before acquire, per reference.
    base_holder = {
        row["public_ref"]: vendor_id
        for vendor_id, row in base["suppliers"].items()
    }
    target_holder = {
        row["public_ref"]: vendor_id
        for vendor_id, row in target["suppliers"].items()
    }
    for public_ref in sorted(set(base_holder) | set(target_holder)):
        old_vendor = base_holder.get(public_ref)
        new_vendor = target_holder.get(public_ref)
        if old_vendor is not None and new_vendor is not None and old_vendor != new_vendor:
            edge((0, old_vendor), (0, new_vendor))

    return nodes, adjacency


def _strongly_connected_components(adjacency):
    """Iterative Kosaraju; components returned with ascending node ids."""
    size = len(adjacency)
    reverse = {i: set() for i in range(size)}
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)

    visited = [False] * size
    finish_order = []
    for start in range(size):
        if visited[start]:
            continue
        visited[start] = True
        stack = [(start, iter(sorted(adjacency[start])))]
        while stack:
            node, iterator = stack[-1]
            advanced = False
            for nxt in iterator:
                if not visited[nxt]:
                    visited[nxt] = True
                    stack.append((nxt, iter(sorted(adjacency[nxt]))))
                    advanced = True
                    break
            if not advanced:
                finish_order.append(node)
                stack.pop()

    components = []
    assigned = [False] * size
    for start in reversed(finish_order):
        if assigned[start]:
            continue
        component = []
        assigned[start] = True
        stack = [start]
        while stack:
            node = stack.pop()
            component.append(node)
            for nxt in reverse[node]:
                if not assigned[nxt]:
                    assigned[nxt] = True
                    stack.append(nxt)
        components.append(sorted(component))
    components.sort(key=lambda component: component[0])
    return components


def _plan_pages(base, target, max_ops):
    supplier_ops, award_ops = _build_operations(base, target)
    if not supplier_ops and not award_ops:
        return []

    nodes, adjacency = _build_precedence_graph(
        base, target, supplier_ops, award_ops
    )
    components = _strongly_connected_components(adjacency)

    oversized = [
        component for component in components if len(component) > max_ops
    ]
    if oversized:
        descriptions = []
        for component in oversized:
            labels = ", ".join(
                _node_label(nodes[node]) for node in component
            )
            descriptions.append(f"[{labels}] ({len(component)} operations)")
        raise PublishError(
            f"cannot publish within max-ops {max_ops}: the following "
            "mutually constrained records must all take effect on one page "
            "and exceed the limit: " + "; ".join(descriptions)
        )

    component_of = {}
    for component_id, component in enumerate(components):
        for node in component:
            component_of[node] = component_id

    indegree = [0] * len(components)
    component_edges = {i: set() for i in range(len(components))}
    for source, targets in adjacency.items():
        source_component = component_of[source]
        for target in targets:
            target_component = component_of[target]
            if target_component != source_component and (
                target_component not in component_edges[source_component]
            ):
                component_edges[source_component].add(target_component)
                indegree[target_component] += 1

    ready = [
        component_id
        for component_id, degree in enumerate(indegree)
        if degree == 0
    ]
    heapq.heapify(ready)
    ordered_components = []
    while ready:
        component_id = heapq.heappop(ready)
        ordered_components.append(component_id)
        for nxt in sorted(component_edges[component_id]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready, nxt)
    if len(ordered_components) != len(components):  # pragma: no cover
        raise PublishError("internal error: publication graph has a cycle")

    pages = []
    current = []
    for component_id in ordered_components:
        component = components[component_id]
        if current and len(current) + len(component) > max_ops:
            pages.append(current)
            current = []
        current.extend(component)
    if current:
        pages.append(current)

    operation_by_node = {}
    for key, operation in supplier_ops.items():
        operation_by_node[(0, key)] = operation
    for key, operation in award_ops.items():
        operation_by_node[(1, key)] = operation
    return [
        [operation_by_node[nodes[node]] for node in page] for page in pages
    ]


def _node_label(node):
    table, key = node
    if table == 0:
        return f"suppliers:{key!r}"
    return f"awards:[{key[0]!r}, {key[1]!r}]"


# ---------------------------------------------------------------------------
# Bundle construction
# ---------------------------------------------------------------------------


def build_bundle(base_mirror, release, max_ops):
    """Build the complete, deterministic page bundle for one release.

    Raises :class:`PublishError` on any invalid input or when the precedence
    constraints cannot be honoured within ``max_ops``; in that case no
    partial result is returned.
    """
    _positive_int(max_ops, "max_ops")
    base = validate_mirror(base_mirror)
    target = validate_release(release)
    if base["stream_id"] != target["stream_id"]:
        raise PublishError(
            f"stream_id mismatch: mirror has {base['stream_id']!r}, release "
            f"has {target['stream_id']!r}"
        )

    planned_pages = _plan_pages(base, target, max_ops)

    pages = []
    current = {
        "schema_version": SCHEMA_VERSION,
        "stream_id": base["stream_id"],
        "position": base["position"],
        "suppliers": {
            key: dict(row) for key, row in base["suppliers"].items()
        },
        "awards": {key: dict(row) for key, row in base["awards"].items()},
    }
    for operations in planned_pages:
        validated = [
            _validate_operation(operation, "planned page.operations")
            for operation in operations
        ]
        before_sha = mirror_sha256(current)
        next_state = _apply_operations(current, validated)
        after_sha = mirror_sha256(next_state)
        pages.append(
            {
                "schema_version": SCHEMA_VERSION,
                "stream_id": base["stream_id"],
                "from_position": current["position"],
                "before_sha256": before_sha,
                "after_sha256": after_sha,
                "operations": copy.deepcopy(operations),
            }
        )
        current = next_state

    # Defensive reconstruction check: the full diff must restore the target.
    if current["suppliers"] != target["suppliers"] or (
        current["awards"] != target["awards"]
    ):
        raise PublishError(  # pragma: no cover
            "internal error: planned pages do not reconstruct the release"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "stream_id": base["stream_id"],
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# Page reception
# ---------------------------------------------------------------------------


def _validate_operation(operation, context):
    if not isinstance(operation, dict):
        raise PublishError(f"{context} must be an object")
    missing = sorted(_OPERATION_FIELDS - set(operation) - {"row"})
    if missing:
        raise PublishError(
            f"{context} is missing required field(s) {', '.join(missing)}"
        )
    extra = sorted(set(operation) - _OPERATION_FIELDS)
    if extra:
        raise PublishError(
            f"{context} has unknown field(s) {', '.join(extra)}"
        )
    table = operation["table"]
    if table not in (TABLE_SUPPLIERS, TABLE_AWARDS):
        raise PublishError(
            f"{context}.table must be 'suppliers' or 'awards', got {table!r}"
        )
    action = operation["action"]
    if action not in (ACTION_UPSERT, ACTION_DELETE):
        raise PublishError(
            f"{context}.action must be 'upsert' or 'delete', got {action!r}"
        )
    key = operation["key"]
    if table == TABLE_SUPPLIERS:
        _non_empty_string(key, f"{context}.key")
        tuple_key = (key,)
    else:
        if not isinstance(key, list) or len(key) != 2:
            raise PublishError(
                f"{context}.key must be a [package_id, lot_id] pair"
            )
        _non_empty_string(key[0], f"{context}.key[0]")
        _non_empty_string(key[1], f"{context}.key[1]")
        tuple_key = (key[0], key[1])

    has_row = "row" in operation
    if action == ACTION_UPSERT:
        if not has_row:
            raise PublishError(f"{context} upsert requires a row")
        if table == TABLE_SUPPLIERS:
            row = _validate_supplier_row(operation["row"], f"{context}.row")
            if row["vendor_id"] != key:
                raise PublishError(
                    f"{context} row vendor_id {row['vendor_id']!r} does not "
                    f"match key {key!r}"
                )
        else:
            row = _validate_award_row(operation["row"], f"{context}.row")
            if (row["package_id"], row["lot_id"]) != tuple_key:
                raise PublishError(
                    f"{context} row award key "
                    f"[{row['package_id']!r}, {row['lot_id']!r}] does not "
                    f"match key {key!r}"
                )
    elif has_row:
        raise PublishError(f"{context} delete must not carry a row")
    else:
        row = None
    return table, action, tuple_key, row


def _apply_operations(mirror, validated_operations):
    """Apply validated ``(table, action, tuple_key, row)`` tuples."""
    suppliers = {
        key: dict(row) for key, row in mirror["suppliers"].items()
    }
    awards = {key: dict(row) for key, row in mirror["awards"].items()}
    for table, action, key, row in validated_operations:
        if table == TABLE_SUPPLIERS:
            vendor_id = key[0]
            if action == ACTION_UPSERT:
                suppliers[vendor_id] = dict(row)
            else:
                del suppliers[vendor_id]
        else:
            if action == ACTION_UPSERT:
                awards[key] = dict(row)
            else:
                del awards[key]
    return {
        "schema_version": SCHEMA_VERSION,
        "stream_id": mirror["stream_id"],
        "position": mirror["position"] + 1,
        "suppliers": suppliers,
        "awards": awards,
    }


def _is_hex_sha256(value, field):
    if not isinstance(value, str) or len(value) != 64:
        raise PublishError(f"{field} must be a 64-character hex SHA-256")
    if any(char not in "0123456789abcdef" for char in value):
        raise PublishError(f"{field} must be lower-case hexadecimal")


def _is_digest_field(value, field):
    """Envelope-level type check; content is checked when the digest is used."""
    if not isinstance(value, str):
        raise PublishError(f"{field} must be a string SHA-256 digest")


def apply_page(base_mirror, page, max_ops):
    """Validate one page against the current mirror and return its successor.

    The mirror is never mutated.  Validation uses only the page and the
    mirror: stream, position, limit, before hash, operation sanity against
    the current rows, and finally references, public-reference uniqueness,
    and the after hash on the complete candidate mirror.
    """
    _positive_int(max_ops, "max_ops")
    base = validate_mirror(base_mirror)

    if not isinstance(page, dict):
        raise PublishError("page must be a JSON object")
    _reject_floats(page, "page")
    _exact_fields(page, _PAGE_FIELDS, "page")
    _schema_one(page["schema_version"], "page")
    stream_id = _non_empty_string(page["stream_id"], "page.stream_id")
    _non_negative_int(page["from_position"], "page.from_position")
    _is_digest_field(page["before_sha256"], "page.before_sha256")
    _is_digest_field(page["after_sha256"], "page.after_sha256")
    operations = page["operations"]
    if not isinstance(operations, list):
        raise PublishError("page.operations must be a list")
    if not operations:
        raise PublishError("page.operations must not be empty")
    if len(operations) > max_ops:
        raise PublishError(
            f"page carries {len(operations)} operations but max-ops is "
            f"{max_ops}"
        )

    if stream_id != base["stream_id"]:
        raise PublishError(
            f"stream_id mismatch: mirror has {base['stream_id']!r}, page has "
            f"{stream_id!r}"
        )
    if page["from_position"] != base["position"]:
        raise PublishError(
            f"position mismatch: mirror is at position {base['position']} "
            f"but page claims from_position {page['from_position']}"
        )

    seen_keys = set()
    validated_operations = []
    for index, operation in enumerate(operations):
        context = f"page.operations[{index}]"
        table, action, key, row = _validate_operation(operation, context)
        if (table, key) in seen_keys:
            if table == TABLE_SUPPLIERS:
                label = f"supplier {key[0]!r}"
            else:
                label = f"award [{key[0]!r}, {key[1]!r}]"
            raise PublishError(f"{context} repeats operation key for {label}")
        seen_keys.add((table, key))

        if table == TABLE_SUPPLIERS:
            existing = base["suppliers"].get(key[0])
            if action == ACTION_DELETE:
                if existing is None:
                    raise PublishError(
                        f"{context} deletes supplier {key[0]!r} that does not "
                        "exist in the mirror"
                    )
            elif existing == row:
                raise PublishError(
                    f"{context} upserts supplier {key[0]!r} with an unchanged row"
                )
        else:
            existing = base["awards"].get(key)
            if action == ACTION_DELETE:
                if existing is None:
                    raise PublishError(
                        f"{context} deletes award [{key[0]!r}, {key[1]!r}] "
                        "that does not exist in the mirror"
                    )
            elif existing == row:
                raise PublishError(
                    f"{context} upserts award [{key[0]!r}, {key[1]!r}] with "
                    "an unchanged row"
                )
        validated_operations.append((table, action, key, row))

    _is_hex_sha256(page["before_sha256"], "page.before_sha256")
    before_sha = mirror_sha256(base)
    if before_sha != page["before_sha256"]:
        raise PublishError(
            "page.before_sha256 does not match the current mirror: mirror "
            f"digest is {before_sha}"
        )

    candidate = _apply_operations(base, validated_operations)
    # Full invariant check on the candidate, including every reference and
    # the uniqueness of public references at the page boundary.
    _validate_keyed_invariants(candidate)

    _is_hex_sha256(page["after_sha256"], "page.after_sha256")
    after_sha = mirror_sha256(candidate)
    if after_sha != page["after_sha256"]:
        raise PublishError(
            "page.after_sha256 does not match the resulting mirror: computed "
            f"digest is {after_sha}"
        )
    return mirror_to_payload(candidate)


def _validate_keyed_invariants(mirror):
    """Check references and public-ref uniqueness on a keyed mirror state."""
    public_refs = {}
    for vendor_id, row in mirror["suppliers"].items():
        public_ref = row["public_ref"]
        if public_ref in public_refs:
            raise PublishError(
                f"resulting mirror reuses public_ref {public_ref!r} for "
                f"vendors {public_refs[public_ref]!r} and {vendor_id!r}"
            )
        public_refs[public_ref] = vendor_id
    for key, row in mirror["awards"].items():
        if row["vendor_id"] not in mirror["suppliers"]:
            raise PublishError(
                "resulting mirror leaves award "
                f"[{key[0]!r}, {key[1]!r}] referencing unknown supplier "
                f"{row['vendor_id']!r}"
            )


def validate_bundle(bundle):
    """Validate the envelope of a bundle without applying any page."""
    if not isinstance(bundle, dict):
        raise PublishError("bundle must be a JSON object")
    _reject_floats(bundle, "bundle")
    _exact_fields(bundle, _BUNDLE_FIELDS, "bundle")
    _schema_one(bundle["schema_version"], "bundle")
    _non_empty_string(bundle["stream_id"], "bundle.stream_id")
    if not isinstance(bundle["pages"], list):
        raise PublishError("bundle.pages must be a list")
    return bundle
