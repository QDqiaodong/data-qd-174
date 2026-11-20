import re
from datetime import date, datetime

from .qualification import (
    QualificationError,
    evidence_entry,
    parse_as_of,
    validate_qualification,
    verify_document,
    visible_credentials,
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _required(mapping, key, context):
    if not isinstance(mapping, dict):
        raise ValueError(f"{context} must be an object")
    if key not in mapping:
        raise ValueError(f"{context} is missing {key}")
    return mapping[key]


def _dt(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamps require an offset")
    return result


def _iso_date(value, field):
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f"{field} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid YYYY-MM-DD date") from exc


def _non_negative_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _peak_concurrency(intervals):
    """Maximum number of half-open intervals sharing a date.

    An interval ending on a date and one starting on that same date do not
    overlap, so end events are processed before start events.
    """
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda item: (item[0], item[1]))
    current = 0
    peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _sequence_key(selection, lot_ids):
    return tuple(
        (0, "") if selection.get(lot_id) is None else (1, selection[lot_id]["offer_id"])
        for lot_id in lot_ids
    )


def _choose_awards(lot_ids, grouped, lots, budget, vendors):
    """Exact search for the lexicographically best feasible award plan.

    Objectives, in order: most awarded lots, lowest total price, then the
    selected offer-ID sequence in ascending lot-ID order with unawarded lots
    ordered before awarded ones.
    """
    count_left = len(lot_ids)
    possible_suffix = [0] * (count_left + 1)
    min_price_suffix = [0] * (count_left + 1)
    for index in range(count_left - 1, -1, -1):
        options = grouped[lot_ids[index]]
        possible_suffix[index] = possible_suffix[index + 1] + (1 if options else 0)
        min_price_suffix[index] = min_price_suffix[index + 1] + (
            min(option["price_cents"] for option in options) if options else 0
        )

    vendor_intervals = {vendor_id: [] for vendor_id in vendors}
    selection = {}
    best = {"key": None, "count": -1, "total": 0, "selection": None}

    def visit(index, awarded, total):
        if best["count"] >= 0:
            if awarded + possible_suffix[index] < best["count"]:
                return
            if (
                awarded + possible_suffix[index] == best["count"]
                and total + min_price_suffix[index] > best["total"]
            ):
                return
        if index == count_left:
            key = (-awarded, total, _sequence_key(selection, lot_ids))
            if best["key"] is None or key < best["key"]:
                best.update(key=key, count=awarded, total=total, selection=dict(selection))
            return

        lot_id = lot_ids[index]
        options = sorted(
            grouped[lot_id],
            key=lambda row: (row["price_cents"], row["vendor_id"], row["offer_id"]),
        )
        for offer in options:
            next_total = total + offer["price_cents"]
            if next_total > budget:
                break  # options are sorted ascending, so larger prices cannot fit
            intervals = vendor_intervals[offer["vendor_id"]]
            intervals.append((offer["service_start"], offer["service_end"]))
            capacity = vendors[offer["vendor_id"]]["concurrent_lot_capacity"]
            if _peak_concurrency(intervals) <= capacity:
                selection[lot_id] = offer
                visit(index + 1, awarded + 1, next_total)
                del selection[lot_id]
            intervals.pop()

        visit(index + 1, awarded, total)

    visit(0, 0, 0)
    return best["selection"], best["total"]


def _selection_reasons(offer_row, lot, selection, total, budget, lots, vendors):
    """Explain why an eligible offer was not part of the winning package."""
    if not offer_row["eligible"]:
        return []
    chosen = selection.get(lot["id"])
    if chosen is offer_row:
        return []

    alternative_total = total - (chosen["price_cents"] if chosen else 0) + offer_row["price_cents"]
    intervals = {vendor_id: [] for vendor_id in vendors}
    for lot_id, selected in selection.items():
        if selected is chosen:
            continue
        selected_lot = lots[lot_id]
        intervals[selected["vendor_id"]].append(
            (selected_lot["service_start"], selected_lot["service_end"])
        )
    intervals[offer_row["vendor_id"]].append((lot["service_start"], lot["service_end"]))

    reasons = []
    if alternative_total > budget:
        reasons.append("package_budget")
    if any(
        _peak_concurrency(vendor_intervals) > vendors[vendor_id]["concurrent_lot_capacity"]
        for vendor_id, vendor_intervals in intervals.items()
    ):
        reasons.append("vendor_capacity")
    if not reasons:
        reasons.append("objective_tradeoff")
    return reasons


def review(data, qualification=None, qualification_as_of=None):
    """Review one procurement package.

    ``qualification`` and ``qualification_as_of`` are optional and must be
    supplied together.  Without them the result is exactly the historical
    review output; no qualification fields are added.
    """
    if (qualification is None) != (qualification_as_of is None):
        raise QualificationError(
            "qualification and qualification_as_of must be provided together"
        )
    qualification_enabled = qualification is not None
    if not isinstance(data, dict):
        raise ValueError("input must be a JSON object")
    for key in ("lots", "offers", "vendors"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"{key} must be a list")
    if "package_budget_cents" not in data:
        raise ValueError("missing package_budget_cents")

    if len({_required(item, "id", "lot") for item in data["lots"]}) != len(
        data["lots"]
    ) or len({_required(item, "id", "offer") for item in data["offers"]}) != len(
        data["offers"]
    ):
        raise ValueError("lot and offer IDs must be unique")

    budget = _non_negative_int(data["package_budget_cents"], "package_budget_cents")

    vendors = {}
    for vendor in data["vendors"]:
        vendor_id = _required(vendor, "id", "vendor")
        if vendor_id in vendors:
            raise ValueError("vendor IDs must be unique")
        capacity = _positive_int(
            _required(vendor, "concurrent_lot_capacity", f"vendor {vendor_id}"),
            f"vendor {vendor_id} concurrent_lot_capacity",
        )
        vendors[vendor_id] = {"id": vendor_id, "concurrent_lot_capacity": capacity}

    as_of = None
    as_of_text = None
    credential_index = None
    if qualification_enabled:
        as_of, as_of_text = parse_as_of(qualification_as_of)
        credential_index = visible_credentials(
            validate_qualification(qualification, vendors), as_of
        )

    lots = {}
    for lot in data["lots"]:
        lot_id = _required(lot, "id", "lot")
        service_start_raw = _required(lot, "service_start", f"lot {lot_id}")
        service_end_raw = _required(lot, "service_end", f"lot {lot_id}")
        service_start = _iso_date(service_start_raw, f"lot {lot_id} service_start")
        service_end = _iso_date(service_end_raw, f"lot {lot_id} service_end")
        if service_start >= service_end:
            raise ValueError(
                f"lot {lot_id} service_start must be before service_end"
            )
        lots[lot_id] = {
            "id": lot_id,
            "deadline": _dt(_required(lot, "deadline", f"lot {lot_id}")),
            "budget_cents": int(_required(lot, "budget_cents", f"lot {lot_id}")),
            "required_documents": set(
                _required(lot, "required_documents", f"lot {lot_id}")
            ),
            "service_start": service_start,
            "service_end": service_end,
            "service_start_text": service_start.isoformat(),
            "service_end_text": service_end.isoformat(),
        }

    grouped = {lot_id: [] for lot_id in lots}
    for offer in data["offers"]:
        offer_id = _required(offer, "id", "offer")
        lot_id = _required(offer, "lot_id", f"offer {offer_id}")
        if lot_id not in lots:
            raise ValueError(f"unknown lot {lot_id}")
        vendor_id = _required(offer, "vendor_id", f"offer {offer_id}")
        if vendor_id not in vendors:
            raise ValueError(f"unknown vendor {vendor_id}")
        price = int(_required(offer, "price_cents", f"offer {offer_id}"))
        if price < 0:
            raise ValueError("offer price cannot be negative")

        lot = lots[lot_id]
        submitted = _dt(_required(offer, "submitted", f"offer {offer_id}"))
        reasons = []
        if submitted > lot["deadline"]:
            reasons.append("late")
        documents = _required(offer, "documents", f"offer {offer_id}")
        missing = sorted(lot["required_documents"] - set(documents))
        if missing:
            reasons.append("missing_documents:" + ",".join(missing))

        qualification_evidence = []
        if qualification_enabled:
            declared_required = set(documents) & lot["required_documents"]
            unverified = []
            for code in sorted(declared_required):
                credential, failure = verify_document(
                    credential_index, vendor_id, code, submitted
                )
                qualification_evidence.append(
                    evidence_entry(code, credential, failure)
                )
                if failure is not None:
                    unverified.append(code)
            if unverified:
                reasons.append("unverified_documents:" + ",".join(unverified))

        if price > lot["budget_cents"]:
            reasons.append("over_budget")
        grouped[lot_id].append(
            {
                "offer_id": offer_id,
                "lot_id": lot_id,
                "vendor_id": vendor_id,
                "price_cents": price,
                "eligible": not reasons,
                "reasons": reasons,
                "service_start": lot["service_start"],
                "service_end": lot["service_end"],
                "qualification_evidence": qualification_evidence,
            }
        )

    lot_ids = sorted(lots)
    eligible_by_lot = {
        lot_id: [row for row in grouped[lot_id] if row["eligible"]] for lot_id in lot_ids
    }
    selection, total = _choose_awards(lot_ids, eligible_by_lot, lots, budget, vendors)

    result_lots = []
    for lot_id in lot_ids:
        lot = lots[lot_id]
        chosen = selection.get(lot_id)
        rows = sorted(
            grouped[lot_id],
            key=lambda row: (row["price_cents"], row["vendor_id"], row["offer_id"]),
        )
        output_rows = []
        for row in rows:
            awarded = row is chosen
            output_row = {
                "offer_id": row["offer_id"],
                "vendor_id": row["vendor_id"],
                "price_cents": row["price_cents"],
                "eligible": row["eligible"],
                "reasons": row["reasons"],
                "awarded": awarded,
                "selection_reasons": (
                    []
                    if awarded
                    else _selection_reasons(
                        row, lot, selection, total, budget, lots, vendors
                    )
                ),
            }
            if qualification_enabled:
                output_row["qualification_evidence"] = row["qualification_evidence"]
            output_rows.append(output_row)
        result_lots.append(
            {
                "lot_id": lot_id,
                "service_start": lot["service_start_text"],
                "service_end": lot["service_end_text"],
                "budget_cents": lot["budget_cents"],
                "offers": output_rows,
                "award_offer_id": chosen["offer_id"] if chosen else None,
                "award_vendor_id": chosen["vendor_id"] if chosen else None,
                "awarded": chosen is not None,
            }
        )

    vendor_summary = []
    for vendor_id in sorted(vendors):
        awarded_lot_ids = [
            lot_id
            for lot_id in lot_ids
            if selection.get(lot_id) is not None
            and selection[lot_id]["vendor_id"] == vendor_id
        ]
        intervals = [
            (lots[lot_id]["service_start"], lots[lot_id]["service_end"])
            for lot_id in awarded_lot_ids
        ]
        vendor_summary.append(
            {
                "vendor_id": vendor_id,
                "concurrent_lot_capacity": vendors[vendor_id]["concurrent_lot_capacity"],
                "peak_concurrent_lots": _peak_concurrency(intervals),
                "awarded_lot_ids": awarded_lot_ids,
            }
        )

    awarded_count = sum(selection.get(lot_id) is not None for lot_id in lot_ids)
    result = {
        "lots": result_lots,
        "awarded": awarded_count,
        "package": {
            "budget_cents": budget,
            "awarded_total_cents": total,
            "remaining_cents": budget - total,
            "lots_total": len(lot_ids),
            "lots_awarded": awarded_count,
            "vendors": vendor_summary,
        },
    }
    if qualification_enabled:
        result["qualification_enabled"] = True
        result["qualification_as_of"] = as_of_text
    return result
