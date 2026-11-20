"""Persistent post-award settlement ledger for procurement packages.

The ledger is a single JSON state file replaced atomically, so accepted events,
batch replay records, and their reports become durable in one commit.  Public
entry points are :func:`initialize_ledger` and :func:`apply_batch`; the
``bid-settle`` CLI is a thin wrapper around them.

All amounts are integer cents.  All money arithmetic is exact.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path

from .core import review

STATE_SCHEMA = "bid-settle-ledger"
STATE_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DAY = timedelta(days=1)

_KINDS = ("receipt", "invoice", "settle")
_EVENT_FIELDS = {
    "receipt": frozenset({"receipt_id", "revision", "lot_id", "accepted_ranges"}),
    "invoice": frozenset({"invoice_id", "lot_id", "from_date", "to_date"}),
    "settle": frozenset({"cash_budget_cents"}),
}


class SettleError(ValueError):
    """A business or input error.  The ledger has not been changed."""


class StateError(SettleError):
    """The existing state is missing, corrupt, or of an unknown schema."""


class BatchError(SettleError):
    """A batch was rejected in its entirety; nothing was committed."""


class ExportError(SettleError):
    """The batch was committed but its report could not be exported.

    ``report`` is the durable report, ``batch_id`` identifies it, and
    ``batch_committed`` is always true.  Re-applying the identical batch is
    safe and will not execute or pay anything again.
    """

    def __init__(self, message, report, batch_id):
        super().__init__(message)
        self.report = copy.deepcopy(report)
        self.batch_id = batch_id
        self.batch_committed = True


# ---------------------------------------------------------------------------
# Strict JSON loading


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SettleError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise SettleError(f"non-finite JSON number {name} is not allowed")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise SettleError(f"non-finite JSON number {text} is not allowed")
    return value


def load_json_text(text, source):
    """Parse strict JSON, rejecting duplicate keys and non-finite numbers."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except SettleError as exc:
        raise SettleError(f"{source}: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise SettleError(f"{source}: invalid JSON: {exc}") from exc


def load_json_file(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettleError(f"cannot read {path}: {exc}") from exc
    return load_json_text(text, str(path))


# ---------------------------------------------------------------------------
# Validation helpers


def _require_object(value, field):
    if not isinstance(value, dict):
        raise SettleError(f"{field} must be an object")
    return value


def _require_finite_numbers(value, field):
    """Reject non-finite floats anywhere in a programmatic JSON object."""
    if isinstance(value, float) and not math.isfinite(value):
        raise SettleError(f"{field} contains a non-finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            _require_finite_numbers(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_finite_numbers(item, f"{field}[{index}]")


def _require_keys(mapping, required, field):
    missing = sorted(required - set(mapping))
    if missing:
        raise SettleError(f"{field} is missing {', '.join(missing)}")
    extra = sorted(set(mapping) - set(required))
    if extra:
        raise SettleError(f"{field} has unknown field(s) {', '.join(extra)}")


def _require_id(value, field):
    if not isinstance(value, str) or not value:
        raise SettleError(f"{field} must be a non-empty string")
    return value


def _require_int(value, field, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettleError(f"{field} must be an integer")
    if value < minimum:
        raise SettleError(f"{field} must be at least {minimum}")
    return value


def _parse_date(value, field):
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise SettleError(f"{field} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SettleError(f"{field} must be a valid YYYY-MM-DD date") from exc


def _day_range(start, end, field):
    if start > end:
        raise SettleError(f"{field} from_date must not be after to_date")
    return start, end


def _each_day(start, end):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += _DAY


# ---------------------------------------------------------------------------
# Contract construction


def daily_amounts(service_start, service_end, total_cents):
    """Split ``total_cents`` over the half-open service interval.

    The interval ``[start, end)`` covers ``D = end - start`` calendar days.
    Every day receives ``total // D`` cents; the earliest ``total % D`` days
    receive one extra cent each.  The sum always equals ``total_cents``,
    including for a zero-price contract.
    """
    start = _parse_date(service_start, "service_start")
    end = _parse_date(service_end, "service_end")
    if start >= end:
        raise SettleError("service_start must be before service_end")
    count = (end - start).days
    base, remainder = divmod(total_cents, count)
    amounts = {}
    for index, day in enumerate(_each_day(start, end - _DAY)):
        amounts[day] = base + (1 if index < remainder else 0)
    return amounts


def _awarded_contracts(award):
    contracts = {}
    for lot in award.get("lots", []):
        if not lot.get("awarded"):
            continue
        offer = next(
            row for row in lot["offers"] if row["offer_id"] == lot["award_offer_id"]
        )
        lot_id = lot["lot_id"]
        daily = daily_amounts(
            lot["service_start"], lot["service_end"], offer["price_cents"]
        )
        contracts[lot_id] = {
            "lot_id": lot_id,
            "vendor_id": lot["award_vendor_id"],
            "offer_id": lot["award_offer_id"],
            "price_cents": offer["price_cents"],
            "service_start": lot["service_start"],
            "service_end": lot["service_end"],
            "daily": {day.isoformat(): cents for day, cents in daily.items()},
        }
    return contracts


# ---------------------------------------------------------------------------
# Atomic file helpers


def _write_temp_file(path, data):
    """Write ``data`` to a temporary file beside ``path``; return its name."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".bid-settle-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return tmp_name


def _publish_new_file(path, data, already_exists_message):
    """Publish ``data`` only if ``path`` does not exist."""
    path = Path(path)
    tmp_name = _write_temp_file(path, data)
    try:
        try:
            os.link(tmp_name, path)
        except FileExistsError as exc:
            raise StateError(already_exists_message) from exc
        except OSError:
            # Filesystem without hard links.  The writer is single, so check
            # then rename; the data was already written completely.
            if path.exists():
                raise StateError(already_exists_message)
            os.replace(tmp_name, path)
            tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _canonical(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SettleError(f"batch is not canonical JSON: {exc}") from exc


def _state_bytes(state):
    return (
        json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode()


def _report_bytes(report):
    return (
        json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode()


# ---------------------------------------------------------------------------
# State loading and validation


def _load_state(path):
    path = Path(path)
    if not path.exists():
        raise StateError(f"state {path} does not exist; run bid-settle init first")
    if path.is_dir():
        raise StateError(f"state {path} is a directory, not a ledger")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"cannot read state {path}: {exc}") from exc
    try:
        state = load_json_text(raw, f"state {path}")
    except SettleError as exc:
        raise StateError(str(exc)) from exc
    try:
        _validate_state(state)
    except StateError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise StateError(f"state {path} is malformed: {exc}") from exc
    return state


def _validate_state(state):
    if not isinstance(state, dict):
        raise StateError("state must be a JSON object")
    if state.get("schema") != STATE_SCHEMA:
        raise StateError(f"unknown state schema {state.get('schema')!r}")
    version = state.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != STATE_SCHEMA_VERSION
    ):
        raise StateError(f"unsupported state schema_version {version!r}")
    for key in (
        "source",
        "award",
        "lots",
        "contracts",
        "receipts",
        "invoices",
        "events",
        "batches",
        "next_seq",
    ):
        if key not in state:
            raise StateError(f"state is incomplete: missing {key}")
    for key in ("lots", "contracts", "receipts", "invoices", "events", "batches"):
        if not isinstance(state[key], dict):
            raise StateError(f"state {key} must be an object")
    if not isinstance(state["next_seq"], int) or isinstance(state["next_seq"], bool):
        raise StateError("state next_seq must be an integer")
    if set(state["lots"]) != set(state["contracts"]):
        raise StateError("state lots and contracts disagree")
    for contract in state["contracts"].values():
        for field in (
            "lot_id",
            "vendor_id",
            "offer_id",
            "price_cents",
            "service_start",
            "service_end",
            "daily",
        ):
            if field not in contract:
                raise StateError("state contract is incomplete")
        if not isinstance(contract["daily"], dict):
            raise StateError("state contract daily amounts must be an object")
        if sum(contract["daily"].values()) != contract["price_cents"]:
            raise StateError("state contract daily amounts do not sum to its price")
    for receipt in state["receipts"].values():
        for field in ("receipt_id", "lot_id", "revision", "accepted_ranges", "seq"):
            if field not in receipt:
                raise StateError("state receipt is incomplete")
        if not isinstance(receipt["accepted_ranges"], list):
            raise StateError("state receipt accepted_ranges must be an array")
        if not isinstance(receipt["revision"], int) or isinstance(receipt["revision"], bool):
            raise StateError("state receipt revision must be an integer")
        if receipt["lot_id"] not in state["contracts"]:
            raise StateError("state receipt references an unknown lot")
    for invoice in state["invoices"].values():
        for field in (
            "invoice_id",
            "lot_id",
            "vendor_id",
            "from_date",
            "to_date",
            "seq",
            "claimed_cents",
            "cash_paid_cents",
            "offset_in_cents",
            "recovered_cents",
            "cash_entries",
            "offset_in_entries",
            "offset_out_entries",
        ):
            if field not in invoice:
                raise StateError("state invoice is incomplete")
        if invoice["lot_id"] not in state["contracts"]:
            raise StateError("state invoice references an unknown lot")
        for field in (
            "seq",
            "claimed_cents",
            "cash_paid_cents",
            "offset_in_cents",
            "recovered_cents",
        ):
            if not isinstance(invoice[field], int) or isinstance(invoice[field], bool):
                raise StateError(f"state invoice {field} must be an integer")
        for field in ("cash_entries", "offset_in_entries", "offset_out_entries"):
            if not isinstance(invoice[field], list):
                raise StateError(f"state invoice {field} must be an array")
        if invoice["cash_paid_cents"] != sum(
            row.get("amount_cents", 0) for row in invoice["cash_entries"]
        ):
            raise StateError("state invoice cash history does not match its total")
        if invoice["offset_in_cents"] != sum(
            row.get("amount_cents", 0) for row in invoice["offset_in_entries"]
        ):
            raise StateError("state invoice offset history does not match its total")
        if invoice["recovered_cents"] != sum(
            row.get("amount_cents", 0) for row in invoice["offset_out_entries"]
        ):
            raise StateError("state invoice recovery history does not match its total")
    for batch_id, record in state["batches"].items():
        if not isinstance(record, dict):
            raise StateError(f"state batch {batch_id} is malformed")
        for key in ("fingerprint", "report", "event_ids"):
            if key not in record:
                raise StateError(f"state batch {batch_id} is missing {key}")
        if not isinstance(record["event_ids"], list):
            raise StateError(f"state batch {batch_id} event_ids must be an array")
        if not isinstance(record["report"], dict):
            raise StateError(f"state batch {batch_id} report must be an object")


# ---------------------------------------------------------------------------
# Initialization


def initialize_ledger(input_path, state_path):
    """Freeze one review result in a new ledger.  Refuses an existing state."""
    state_path = Path(state_path)
    if state_path.exists():
        raise StateError(f"state {state_path} already exists; refusing to reinitialise")

    package = load_json_file(input_path)
    if not isinstance(package, dict):
        raise SettleError("package input must be a JSON object")
    try:
        _require_finite_numbers(package, "package")
    except SettleError as exc:
        raise SettleError(f"invalid package: {exc}") from exc
    try:
        award = review(copy.deepcopy(package))
    except (ValueError, KeyError, TypeError) as exc:
        raise SettleError(f"invalid package: {exc}") from exc

    contracts = _awarded_contracts(award)
    lots = {
        lot_id: {
            "lot_id": lot_id,
            "vendor_id": contract["vendor_id"],
            "offer_id": contract["offer_id"],
            "price_cents": contract["price_cents"],
            "service_start": contract["service_start"],
            "service_end": contract["service_end"],
        }
        for lot_id, contract in contracts.items()
    }
    state = {
        "schema": STATE_SCHEMA,
        "schema_version": STATE_SCHEMA_VERSION,
        "source": {
            "input_path": os.path.abspath(str(input_path)),
            "package": package,
            "award": award,
        },
        "award": {
            "awarded": award["awarded"],
            "package_budget_cents": award["package"]["budget_cents"],
            "awarded_total_cents": award["package"]["awarded_total_cents"],
        },
        "lots": lots,
        "contracts": contracts,
        "receipts": {},
        "invoices": {},
        "events": {},
        "batches": {},
        "next_seq": 1,
    }
    # Validate in memory before publishing so a failed init leaves no state.
    _validate_state(state)
    data = _state_bytes(state)
    try:
        _publish_new_file(
            state_path,
            data,
            f"state {state_path} already exists; refusing to reinitialise",
        )
    except OSError as exc:
        raise SettleError(f"cannot create state {state_path}: {exc}") from exc
    return state


# ---------------------------------------------------------------------------
# Batch parsing and structural validation


def _validate_contract_span(start, end, lot, field):
    """Reject a date span outside the half-open contract service interval."""
    service_start = date.fromisoformat(lot["service_start"])
    service_end = date.fromisoformat(lot["service_end"])
    if start < service_start or end >= service_end:
        raise SettleError(
            f"{field} dates {start.isoformat()}..{end.isoformat()} fall outside "
            f"lot {lot['lot_id']} service interval "
            f"{lot['service_start']}..{lot['service_end']} (end exclusive)"
        )


def _validate_lot_reference(lot_id, field, lots):
    _require_id(lot_id, field)
    if lot_id not in lots:
        known = ", ".join(sorted(lots)) or "(none)"
        raise SettleError(
            f"{field} references unknown or unawarded lot {lot_id!r}; "
            f"awarded lots: {known}"
        )


def _validate_event(event, index, lots):
    field = f"events[{index}]"
    _require_object(event, field)
    if "event_id" not in event:
        raise SettleError(f"{field} is missing event_id")
    if "kind" not in event:
        raise SettleError(f"{field} is missing kind")
    event_id = _require_id(event["event_id"], f"{field}.event_id")
    kind = event["kind"]
    if kind not in _KINDS:
        raise SettleError(f"{field} ({event_id}) has unknown kind {kind!r}")
    expected = _EVENT_FIELDS[kind] | {"event_id", "kind"}
    _require_keys(event, expected, f"{field} ({event_id})")

    if kind == "receipt":
        _require_id(event["receipt_id"], f"{field}.receipt_id")
        _require_int(event["revision"], f"{field}.revision", minimum=1)
        _validate_lot_reference(event["lot_id"], f"{field}.lot_id", lots)
        ranges = event["accepted_ranges"]
        if not isinstance(ranges, list):
            raise SettleError(f"{field}.accepted_ranges must be an array")
        parsed = []
        for r_index, item in enumerate(ranges):
            rfield = f"{field}.accepted_ranges[{r_index}]"
            _require_object(item, rfield)
            _require_keys(item, frozenset({"from_date", "to_date"}), rfield)
            start = _parse_date(item["from_date"], f"{rfield}.from_date")
            end = _parse_date(item["to_date"], f"{rfield}.to_date")
            _day_range(start, end, rfield)
            _validate_contract_span(start, end, lots[event["lot_id"]], rfield)
            parsed.append((start, end))
        parsed.sort()
        for previous, current in zip(parsed, parsed[1:]):
            if current[0] <= previous[1]:
                raise SettleError(
                    f"{field}.accepted_ranges contains overlapping ranges "
                    f"{previous[0].isoformat()}..{previous[1].isoformat()} and "
                    f"{current[0].isoformat()}..{current[1].isoformat()}"
                )
    elif kind == "invoice":
        _require_id(event["invoice_id"], f"{field}.invoice_id")
        _validate_lot_reference(event["lot_id"], f"{field}.lot_id", lots)
        start = _parse_date(event["from_date"], f"{field}.from_date")
        end = _parse_date(event["to_date"], f"{field}.to_date")
        _day_range(start, end, field)
        _validate_contract_span(start, end, lots[event["lot_id"]], field)
    else:
        _require_int(event["cash_budget_cents"], f"{field}.cash_budget_cents", minimum=0)
    return event_id, kind


def _parse_batch(batch, lots):
    _require_object(batch, "batch")
    _require_keys(batch, frozenset({"batch_id", "events"}), "batch")
    batch_id = _require_id(batch["batch_id"], "batch.batch_id")
    events = batch["events"]
    if not isinstance(events, list):
        raise SettleError("batch.events must be an array")
    seen = set()
    parsed = []
    for index, event in enumerate(events):
        event_id, kind = _validate_event(event, index, lots)
        if event_id in seen:
            raise SettleError(
                f"events[{index}].event_id {event_id!r} is duplicated in the batch"
            )
        seen.add(event_id)
        parsed.append((event_id, kind))
    return batch_id, events, parsed


# ---------------------------------------------------------------------------
# Fact computation


def _accepted_days_by_lot(state):
    accepted = {}
    for receipt in state["receipts"].values():
        days = accepted.setdefault(receipt["lot_id"], set())
        for item in receipt["accepted_ranges"]:
            days.update(
                _each_day(
                    date.fromisoformat(item["from_date"]),
                    date.fromisoformat(item["to_date"]),
                )
            )
    return accepted


def _invoice_days(invoice):
    return _each_day(
        date.fromisoformat(invoice["from_date"]),
        date.fromisoformat(invoice["to_date"]),
    )


def _earned_cents(state, invoice, accepted_by_lot):
    daily = state["contracts"][invoice["lot_id"]]["daily"]
    accepted = accepted_by_lot.get(invoice["lot_id"], set())
    return sum(daily[day.isoformat()] for day in _invoice_days(invoice) if day in accepted)


def _net_settled(invoice):
    return (
        invoice["cash_paid_cents"]
        + invoice["offset_in_cents"]
        - invoice["recovered_cents"]
    )


def _money_facts(invoice, earned):
    net = _net_settled(invoice)
    return {
        "earned_cents": earned,
        "net_settled_cents": net,
        "due_cents": max(earned - net, 0),
        "recoverable_cents": max(net - earned, 0),
        "pending_acceptance_cents": invoice["claimed_cents"] - earned,
    }


# ---------------------------------------------------------------------------
# Event application


def _apply_receipt(state, event, seq):
    receipt_id = event["receipt_id"]
    lot_id = event["lot_id"]
    revision = event["revision"]
    existing = state["receipts"].get(receipt_id)
    if existing is None:
        if revision != 1:
            raise SettleError(
                f"receipt {receipt_id!r} is new; revision must be 1, got {revision}"
            )
    else:
        if existing["lot_id"] != lot_id:
            raise SettleError(
                f"receipt {receipt_id!r} belongs to lot {existing['lot_id']!r}; "
                f"lot_id cannot change to {lot_id!r}"
            )
        expected = existing["revision"] + 1
        if revision != expected:
            raise SettleError(
                f"receipt {receipt_id!r} has revision {existing['revision']}; "
                f"expected revision {expected}, got {revision}"
            )
    state["receipts"][receipt_id] = {
        "receipt_id": receipt_id,
        "lot_id": lot_id,
        "revision": revision,
        "accepted_ranges": copy.deepcopy(event["accepted_ranges"]),
        "seq": seq,
    }


def _apply_invoice(state, event, seq):
    invoice_id = event["invoice_id"]
    if invoice_id in state["invoices"]:
        raise SettleError(
            f"invoice {invoice_id!r} already exists at seq "
            f"{state['invoices'][invoice_id]['seq']}; invoice IDs are immutable"
        )
    lot_id = event["lot_id"]
    lot = state["lots"][lot_id]
    start = date.fromisoformat(event["from_date"])
    end = date.fromisoformat(event["to_date"])
    for other in state["invoices"].values():
        if other["lot_id"] != lot_id:
            continue
        other_start = date.fromisoformat(other["from_date"])
        other_end = date.fromisoformat(other["to_date"])
        if start <= other_end and other_start <= end:
            raise SettleError(
                f"invoice {invoice_id!r} date range "
                f"{event['from_date']}..{event['to_date']} overlaps invoice "
                f"{other['invoice_id']!r} ({other['from_date']}..{other['to_date']}) "
                f"on lot {lot_id!r}"
            )
    daily = state["contracts"][lot_id]["daily"]
    claimed = sum(daily[day.isoformat()] for day in _each_day(start, end))
    state["invoices"][invoice_id] = {
        "invoice_id": invoice_id,
        "lot_id": lot_id,
        "vendor_id": lot["vendor_id"],
        "from_date": event["from_date"],
        "to_date": event["to_date"],
        "seq": seq,
        "claimed_cents": claimed,
        "cash_paid_cents": 0,
        "offset_in_cents": 0,
        "recovered_cents": 0,
        "cash_entries": [],
        "offset_in_entries": [],
        "offset_out_entries": [],
    }


def _apply_settle(state, event, seq, batch_id):
    budget = event["cash_budget_cents"]
    accepted = _accepted_days_by_lot(state)
    invoices = sorted(state["invoices"].values(), key=lambda row: row["seq"])
    earned = {
        invoice["invoice_id"]: _earned_cents(state, invoice, accepted)
        for invoice in invoices
    }
    due = {
        invoice["invoice_id"]: _money_facts(invoice, earned[invoice["invoice_id"]])[
            "due_cents"
        ]
        for invoice in invoices
    }
    recoverable = {
        invoice["invoice_id"]: _money_facts(
            invoice, earned[invoice["invoice_id"]]
        )["recoverable_cents"]
        for invoice in invoices
    }

    offsets = []
    vendors = sorted({invoice["vendor_id"] for invoice in invoices})
    for vendor_id in vendors:
        sources = [
            invoice
            for invoice in invoices
            if invoice["vendor_id"] == vendor_id
            and recoverable[invoice["invoice_id"]] > 0
        ]
        targets = [
            invoice
            for invoice in invoices
            if invoice["vendor_id"] == vendor_id and due[invoice["invoice_id"]] > 0
        ]
        for source in sources:
            source_id = source["invoice_id"]
            remaining = recoverable[source_id]
            for target in targets:
                if remaining <= 0:
                    break
                target_id = target["invoice_id"]
                if target_id == source_id or due[target_id] <= 0:
                    continue
                amount = min(remaining, due[target_id])
                if amount <= 0:
                    continue
                remaining -= amount
                due[target_id] -= amount
                recoverable[source_id] -= amount
                source["recovered_cents"] += amount
                target["offset_in_cents"] += amount
                source["offset_out_entries"].append(
                    {
                        "settle_seq": seq,
                        "batch_id": batch_id,
                        "to_invoice_id": target_id,
                        "amount_cents": amount,
                    }
                )
                target["offset_in_entries"].append(
                    {
                        "settle_seq": seq,
                        "batch_id": batch_id,
                        "from_invoice_id": source_id,
                        "amount_cents": amount,
                    }
                )
                offsets.append(
                    {
                        "from_invoice_id": source_id,
                        "to_invoice_id": target_id,
                        "vendor_id": vendor_id,
                        "amount_cents": amount,
                    }
                )

    remaining_budget = budget
    payments = []
    for invoice in invoices:
        if remaining_budget <= 0:
            break
        invoice_id = invoice["invoice_id"]
        facts = _money_facts(invoice, earned[invoice_id])
        if facts["due_cents"] <= 0:
            continue
        amount = min(facts["due_cents"], remaining_budget)
        if amount <= 0:
            continue
        remaining_budget -= amount
        invoice["cash_paid_cents"] += amount
        invoice["cash_entries"].append(
            {
                "settle_seq": seq,
                "batch_id": batch_id,
                "amount_cents": amount,
            }
        )
        payments.append(
            {
                "invoice_id": invoice_id,
                "vendor_id": invoice["vendor_id"],
                "amount_cents": amount,
            }
        )

    spent = budget - remaining_budget
    return {
        "event_id": None,
        "seq": seq,
        "cash_budget_cents": budget,
        "cash_paid_cents": spent,
        "unused_budget_cents": remaining_budget,
        "offset_cents": sum(row["amount_cents"] for row in offsets),
        "offsets": offsets,
        "cash_payments": payments,
    }


def _apply_events(state, batch_id, events, parsed):
    seq_map = []
    settlements = []
    for (event_id, kind), event in zip(parsed, events):
        if event_id in state["events"]:
            raise SettleError(
                f"events[{len(seq_map)}].event_id {event_id!r} was already used at "
                f"seq {state['events'][event_id]} in an earlier batch"
            )
        seq = state["next_seq"]
        if kind == "receipt":
            _apply_receipt(state, event, seq)
        elif kind == "invoice":
            _apply_invoice(state, event, seq)
        else:
            settlement = _apply_settle(state, event, seq, batch_id)
            settlement["event_id"] = event_id
            settlements.append(settlement)
        state["events"][event_id] = seq
        state["next_seq"] = seq + 1
        seq_map.append({"event_id": event_id, "seq": seq, "kind": kind})
    return seq_map, settlements


# ---------------------------------------------------------------------------
# Reporting


def _lot_summary(state, accepted):
    rows = []
    for lot_id in sorted(state["lots"]):
        contract = state["contracts"][lot_id]
        daily = contract["daily"]
        accepted_days = accepted.get(lot_id, set())
        accepted_cents = sum(
            daily[day.isoformat()] for day in accepted_days if day.isoformat() in daily
        )
        invoiced = sum(
            invoice["claimed_cents"]
            for invoice in state["invoices"].values()
            if invoice["lot_id"] == lot_id
        )
        rows.append(
            {
                "lot_id": lot_id,
                "vendor_id": contract["vendor_id"],
                "offer_id": contract["offer_id"],
                "service_start": contract["service_start"],
                "service_end": contract["service_end"],
                "contract_cents": contract["price_cents"],
                "accepted_cents": accepted_cents,
                "invoiced_claimed_cents": invoiced,
                "daily_amounts": dict(sorted(daily.items())),
            }
        )
    return rows


def _invoice_summary(state, accepted):
    rows = []
    for invoice in sorted(state["invoices"].values(), key=lambda row: row["seq"]):
        earned = _earned_cents(state, invoice, accepted)
        facts = _money_facts(invoice, earned)
        rows.append(
            {
                "invoice_id": invoice["invoice_id"],
                "lot_id": invoice["lot_id"],
                "vendor_id": invoice["vendor_id"],
                "seq": invoice["seq"],
                "from_date": invoice["from_date"],
                "to_date": invoice["to_date"],
                "claimed_cents": invoice["claimed_cents"],
                "cash_paid_cents": invoice["cash_paid_cents"],
                "offset_in_cents": invoice["offset_in_cents"],
                "recovered_cents": invoice["recovered_cents"],
                **facts,
            }
        )
    return rows


def _build_report(state, batch_id, seq_map, settlements):
    accepted = _accepted_days_by_lot(state)
    invoices = _invoice_summary(state, accepted)
    cash_budget = sum(row["cash_budget_cents"] for row in settlements)
    cash_paid = sum(row["cash_paid_cents"] for row in settlements)
    unused = sum(row["unused_budget_cents"] for row in settlements)
    offset = sum(row["offset_cents"] for row in settlements)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "batch_id": batch_id,
        "events": seq_map,
        "event_seq": {row["event_id"]: row["seq"] for row in seq_map},
        "settlements": settlements,
        "cash_budget_cents": cash_budget,
        "cash_paid_cents": cash_paid,
        "unused_budget_cents": unused,
        "offset_cents": offset,
        # Explicit long-form totals make reconciliation easy without knowing
        # the short field names used above.
        "total_cash_budget_cents": cash_budget,
        "total_cash_paid_cents": cash_paid,
        "total_unused_budget_cents": unused,
        "total_offset_cents": offset,
        "lots": _lot_summary(state, accepted),
        "invoices": invoices,
    }


# ---------------------------------------------------------------------------
# Apply / export


def _publish_report(output_path, report, batch_id):
    """Publish a complete report without overwriting different bytes."""
    if output_path is None:
        return
    target = Path(output_path)
    data = _report_bytes(report)
    different = ExportError(
        f"report target {target} already exists with different content; "
        f"refusing to overwrite it",
        report,
        batch_id,
    )
    try:
        if target.exists():
            if target.is_file() and target.read_bytes() == data:
                return
            raise different
        tmp_name = _write_temp_file(target, data)
        try:
            try:
                os.link(tmp_name, target)
            except FileExistsError as exc:
                if target.is_file() and target.read_bytes() == data:
                    return
                raise different from exc
            except OSError:
                # Filesystem without hard links.  The writer is single, so a
                # checked rename still publishes the complete temporary file.
                if target.exists():
                    raise different
                os.replace(tmp_name, target)
                tmp_name = None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(
            f"cannot write report {target}: {exc}", report, batch_id
        ) from exc


def apply_batch(state_path, batch, output_path=None):
    """Apply one batch and return the durable report object.

    ``batch`` may be a parsed JSON object or a path to a JSON file.  On exact
    replay of an accepted batch the persisted report is returned without
    executing or paying anything again.
    """
    state = _load_state(state_path)
    if isinstance(batch, (str, os.PathLike)):
        batch = load_json_file(batch)

    # Envelope checks first, then the replay/conflict decision.  An accepted
    # batch must be recognised as a replay even though its old revisions and
    # event IDs are now used facts; only afterwards are events validated.
    try:
        _require_object(batch, "batch")
        _require_finite_numbers(batch, "batch")
        _require_keys(batch, frozenset({"batch_id", "events"}), "batch")
        batch_id = _require_id(batch["batch_id"], "batch.batch_id")
        if not isinstance(batch["events"], list):
            raise SettleError("batch.events must be an array")
    except SettleError as exc:
        raise BatchError(f"batch rejected: {exc}") from exc

    try:
        fingerprint = _canonical(batch)
    except SettleError as exc:
        raise BatchError(f"batch rejected: {exc}") from exc
    previous = state["batches"].get(batch_id)
    if previous is not None:
        if previous["fingerprint"] != fingerprint:
            raise BatchError(
                f"batch {batch_id!r} was already accepted with different content; "
                f"a batch ID cannot be reused"
            )
        # Exact replay: return the persisted report before consulting mutable
        # things such as used revisions or event IDs.
        report = copy.deepcopy(previous["report"])
        _publish_report(output_path, report, batch_id)
        return report

    try:
        batch_id, events, parsed = _parse_batch(batch, state["lots"])
    except SettleError as exc:
        raise BatchError(f"batch {batch_id!r} rejected: {exc}") from exc
    assert batch_id == batch["batch_id"]

    work = copy.deepcopy(state)
    try:
        seq_map, settlements = _apply_events(work, batch_id, events, parsed)
        report = _build_report(work, batch_id, seq_map, settlements)
    except SettleError as exc:
        raise BatchError(f"batch {batch_id!r} rejected: {exc}; nothing was committed") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise BatchError(
            f"batch {batch_id!r} rejected: malformed event data ({exc}); "
            "nothing was committed"
        ) from exc

    work["batches"][batch_id] = {
        "fingerprint": fingerprint,
        "event_ids": [row["event_id"] for row in seq_map],
        "report": report,
    }
    _validate_state(work)
    # The business commit happens before report export.  If export fails the
    # caller gets ExportError saying the batch WAS accepted; replay is safe.
    data = _state_bytes(work)
    try:
        tmp_name = _write_temp_file(state_path, data)
    except OSError as exc:
        raise SettleError(
            f"batch {batch_id!r} could not be committed: {exc}; nothing was committed"
        ) from exc
    try:
        os.replace(tmp_name, state_path)
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise SettleError(
            f"batch {batch_id!r} could not be committed: {exc}; nothing was committed"
        ) from exc
    _publish_report(output_path, report, batch_id)
    return report
