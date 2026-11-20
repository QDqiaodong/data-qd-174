"""Generate the deterministic synthetic response-audit fixtures.

Run from the repository root:

    python3 fixtures/response-audit/generate_fixtures.py

Writes three files and nothing else:

* ``review.json`` — a small archived ``bid-review`` package result with two
  awarded lots (``LOT-A``/``V1`` and ``LOT-B``/``V2``), one unawarded lot, and
  an unawarded offer retained on an awarded lot.  Only the lot and awarded
  vendor identities are used by the audit.
* ``logs.json`` — the small example: the prompt's ambiguous pairs, an
  alternate-offset timestamp, a same-instant pair of distinct records, a
  site number reused under two lots, an empty site, and a check-in that lies
  in a window but cannot join any maximum scheme.
* ``scale-review.json`` / ``scale-logs.json`` — the reproducible 30,000-ticket
  / 30,000-check-in benchmark described in ``docs/response-audit.md``.

The generator contains no clock or randomness: re-running it produces
byte-identical output.  It does not run the audit, so it stays fast even for
the scale case.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

BASE = datetime(2026, 4, 1, tzinfo=timezone.utc)


def ts(minutes=0, seconds=0, *, offset_hours=0):
    instant = BASE + timedelta(minutes=minutes, seconds=seconds)
    zone = timezone(timedelta(hours=offset_hours))
    return instant.astimezone(zone).isoformat()


# --- Small example -------------------------------------------------------


def small_review():
    return {
        "awarded": 2,
        "package": {
            "budget_cents": 90000,
            "awarded_total_cents": 60000,
            "remaining_cents": 30000,
            "lots_total": 3,
            "lots_awarded": 2,
            "vendors": [
                {
                    "vendor_id": "V1",
                    "concurrent_lot_capacity": 1,
                    "peak_concurrent_lots": 1,
                    "awarded_lot_ids": ["LOT-A"],
                },
                {
                    "vendor_id": "V2",
                    "concurrent_lot_capacity": 1,
                    "peak_concurrent_lots": 1,
                    "awarded_lot_ids": ["LOT-B"],
                },
            ],
        },
        "lots": [
            {
                "lot_id": "LOT-A",
                "service_start": "2026-05-01",
                "service_end": "2026-06-01",
                "budget_cents": 50000,
                "offers": [
                    {
                        "offer_id": "OA1",
                        "vendor_id": "V1",
                        "price_cents": 40000,
                        "eligible": True,
                        "reasons": [],
                        "awarded": True,
                        "selection_reasons": [],
                    },
                    {
                        "offer_id": "OA2",
                        "vendor_id": "V2",
                        "price_cents": 42000,
                        "eligible": True,
                        "reasons": [],
                        "awarded": False,
                        "selection_reasons": ["objective_tradeoff"],
                    },
                ],
                "award_offer_id": "OA1",
                "award_vendor_id": "V1",
                "awarded": True,
            },
            {
                "lot_id": "LOT-B",
                "service_start": "2026-05-01",
                "service_end": "2026-06-01",
                "budget_cents": 30000,
                "offers": [
                    {
                        "offer_id": "OB1",
                        "vendor_id": "V2",
                        "price_cents": 20000,
                        "eligible": True,
                        "reasons": [],
                        "awarded": True,
                        "selection_reasons": [],
                    }
                ],
                "award_offer_id": "OB1",
                "award_vendor_id": "V2",
                "awarded": True,
            },
            {
                "lot_id": "LOT-C",
                "service_start": "2026-07-01",
                "service_end": "2026-08-01",
                "budget_cents": 10000,
                "offers": [
                    {
                        "offer_id": "OC1",
                        "vendor_id": "V1",
                        "price_cents": 12000,
                        "eligible": False,
                        "reasons": ["over_budget"],
                        "awarded": False,
                        "selection_reasons": [],
                    }
                ],
                "award_offer_id": None,
                "award_vendor_id": None,
                "awarded": False,
            },
        ],
    }


def small_logs():
    sites = [
        # The prompt's 5-minute SLA / 20-minute search window.
        {"lot_id": "LOT-A", "site_id": "SITE-1", "sla_minutes": 5, "search_minutes": 20},
        {"lot_id": "LOT-A", "site_id": "SITE-2", "sla_minutes": 5, "search_minutes": 20},
        {"lot_id": "LOT-A", "site_id": "SITE-3", "sla_minutes": 5, "search_minutes": 20},
        {"lot_id": "LOT-A", "site_id": "SITE-4", "sla_minutes": 5, "search_minutes": 20},
        {"lot_id": "LOT-A", "site_id": "SITE-5", "sla_minutes": 5, "search_minutes": 20},
        {"lot_id": "LOT-A", "site_id": "SITE-6", "sla_minutes": 5, "search_minutes": 20},
        # Same site number as LOT-A's SITE-1 but a different lot: isolated.
        {"lot_id": "LOT-B", "site_id": "SITE-1", "sla_minutes": 10, "search_minutes": 30},
        # Configured but with no records at all.
        {"lot_id": "LOT-B", "site_id": "SITE-2", "sla_minutes": 10, "search_minutes": 30},
    ]
    tickets = [
        # SITE-1: 09:00 and 09:03, one 09:06 check-in -> max 1, both ambiguous.
        {"ticket_id": "TA1", "lot_id": "LOT-A", "site_id": "SITE-1", "opened_at": ts(9 * 60)},
        {"ticket_id": "TA2", "lot_id": "LOT-A", "site_id": "SITE-1", "opened_at": ts(9 * 60 + 3)},
        # SITE-2: one ticket, 09:02 and 09:04 check-ins -> on_time provable,
        # no confirmed check-in.
        {"ticket_id": "TB1", "lot_id": "LOT-A", "site_id": "SITE-2", "opened_at": ts(9 * 60)},
        # SITE-3: window rejects the 09:08 check-in for TA1 despite it being
        # inside the 20-minute search window; the ticket is printed in a
        # +02:00 offset to prove instant comparison.
        {"ticket_id": "TC1", "lot_id": "LOT-A", "site_id": "SITE-3", "opened_at": ts(9 * 60, offset_hours=2)},
        {"ticket_id": "TC2", "lot_id": "LOT-A", "site_id": "SITE-3", "opened_at": ts(9 * 60 + 5)},
        # SITE-4: same instant written with two offsets is still one fact per
        # record; two distinct records at one instant stay separate facts.
        {"ticket_id": "TD1", "lot_id": "LOT-A", "site_id": "SITE-4", "opened_at": ts(9 * 60, offset_hours=2)},
        {"ticket_id": "TD2", "lot_id": "LOT-A", "site_id": "SITE-4", "opened_at": ts(9 * 60, offset_hours=-5)},
        # SITE-5: one ticket, one on-time and one late candidate; either can
        # be the maximum pairing, so the verdict cannot be confirmed.
        {"ticket_id": "TF1", "lot_id": "LOT-A", "site_id": "SITE-5", "opened_at": ts(9 * 60)},
        # SITE-6: a confirmable late pairing and, at another opening time, a
        # ticket with no candidate check-in at all.
        {"ticket_id": "TG1", "lot_id": "LOT-A", "site_id": "SITE-6", "opened_at": ts(9 * 60)},
        {"ticket_id": "TG2", "lot_id": "LOT-A", "site_id": "SITE-6", "opened_at": ts(10 * 60)},
        # LOT-B SITE-1: same site number, different lot and different award.
        {"ticket_id": "TE1", "lot_id": "LOT-B", "site_id": "SITE-1", "opened_at": ts(9 * 60)},
    ]
    checkins = [
        {"checkin_id": "CA1", "lot_id": "LOT-A", "site_id": "SITE-1", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 6)},
        {"checkin_id": "CB1", "lot_id": "LOT-A", "site_id": "SITE-2", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 2)},
        {"checkin_id": "CB2", "lot_id": "LOT-A", "site_id": "SITE-2", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 4)},
        {"checkin_id": "CC1", "lot_id": "LOT-A", "site_id": "SITE-3", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 1)},
        {"checkin_id": "CC2", "lot_id": "LOT-A", "site_id": "SITE-3", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 8)},
        {"checkin_id": "CD1", "lot_id": "LOT-A", "site_id": "SITE-4", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 1, offset_hours=1)},
        {"checkin_id": "CD2", "lot_id": "LOT-A", "site_id": "SITE-4", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 1, offset_hours=-7)},
        {"checkin_id": "CF1", "lot_id": "LOT-A", "site_id": "SITE-5", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 3)},
        {"checkin_id": "CF2", "lot_id": "LOT-A", "site_id": "SITE-5", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 10)},
        {"checkin_id": "CG1", "lot_id": "LOT-A", "site_id": "SITE-6", "vendor_id": "V1", "arrived_at": ts(9 * 60 + 10)},
        {"checkin_id": "CE1", "lot_id": "LOT-B", "site_id": "SITE-1", "vendor_id": "V2", "arrived_at": ts(9 * 60 + 4)},
    ]
    return {
        "closed_through": "2026-04-30T23:59:59+00:00",
        "sites": sites,
        "tickets": tickets,
        "checkins": checkins,
    }


# --- Scale example -------------------------------------------------------

SCALE_TICKETS = 30000
SCALE_TAIL_CHECKINS = 6


def scale_review():
    return {
        "awarded": 1,
        "package": {
            "budget_cents": 5000000,
            "awarded_total_cents": 4000000,
            "remaining_cents": 1000000,
            "lots_total": 1,
            "lots_awarded": 1,
            "vendors": [
                {
                    "vendor_id": "V1",
                    "concurrent_lot_capacity": 1,
                    "peak_concurrent_lots": 1,
                    "awarded_lot_ids": ["LOT-A"],
                }
            ],
        },
        "lots": [
            {
                "lot_id": "LOT-A",
                "service_start": "2026-01-01",
                "service_end": "2026-04-01",
                "budget_cents": 5000000,
                "offers": [
                    {
                        "offer_id": "O1",
                        "vendor_id": "V1",
                        "price_cents": 4000000,
                        "eligible": True,
                        "reasons": [],
                        "awarded": True,
                        "selection_reasons": [],
                    }
                ],
                "award_offer_id": "O1",
                "award_vendor_id": "V1",
                "awarded": True,
            }
        ],
    }


def scale_logs():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tickets = []
    checkins = []
    # Ticket i opens at 3i minutes.  Check-in j arrives 90 s after 3j
    # minutes, so ticket i's 20-minute window holds check-ins i..i+6.  The
    # extra tail check-ins keep every ticket matchable, and the overlapping
    # windows make each ticket's partner interchangeable across many maximum
    # reconciliations.
    for i in range(SCALE_TICKETS):
        opened = base + timedelta(minutes=3 * i)
        tickets.append(
            {
                "ticket_id": f"T{i:06d}",
                "lot_id": "LOT-A",
                "site_id": "SITE-1",
                "opened_at": opened.isoformat(),
            }
        )
    for j in range(SCALE_TICKETS + SCALE_TAIL_CHECKINS):
        arrived = base + timedelta(minutes=3 * j, seconds=90)
        checkins.append(
            {
                "checkin_id": f"C{j:06d}",
                "lot_id": "LOT-A",
                "site_id": "SITE-1",
                "vendor_id": "V1",
                "arrived_at": arrived.isoformat(),
            }
        )
    return {
        "closed_through": (base + timedelta(days=70)).isoformat(),
        "sites": [
            {
                "lot_id": "LOT-A",
                "site_id": "SITE-1",
                "sla_minutes": 5,
                "search_minutes": 20,
            }
        ],
        "tickets": tickets,
        "checkins": checkins,
    }


def dump(name, value, *, compact=False):
    target = HERE / name
    if compact:
        text = json.dumps(value, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(value, indent=2) + "\n"
    target.write_text(text, encoding="utf-8")


def main():
    dump("review.json", small_review())
    dump("logs.json", small_logs())
    dump("scale-review.json", scale_review())
    dump("scale-logs.json", scale_logs(), compact=True)


if __name__ == "__main__":
    main()
