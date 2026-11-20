"""Regenerate the deterministic synthetic bid-cost-compare fixtures.

Run from the repository root:

    python3 fixtures/cost-compare/generate_fixtures.py

The fixtures are deliberately small and fully synthetic.  The generator
builds the annex, basket and a matching ``bid-review``-shaped result, then
runs the real analysis and writes its result as the expected output, so the
fixture always reflects the current implementation.  It writes only the five
JSON files listed in SOURCES.md and produces byte-identical output when
re-run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from procurement.costcompare import (  # noqa: E402
    compare_bid_costs,
    solve_transportation,
    verify_allocation,
)

HERE = Path(__file__).resolve().parent

SERVICES = [
    {
        "service_id": "P",
        "base_unit": "hour",
        "units": [
            # One 8-hour day equals eight base hours.
            {"unit": "day", "numerator": 8, "denominator": 1},
        ],
    },
    {
        "service_id": "Q",
        "base_unit": "visit",
        "units": [
            # A block of six visits; one block equals six base visits.
            {"unit": "block", "numerator": 6, "denominator": 1},
        ],
    },
    {
        "service_id": "R",
        "base_unit": "hour",
        "units": [
            # Half-day units: one unit equals four base hours.
            {"unit": "half_day", "numerator": 4, "denominator": 1},
        ],
    },
    {
        "service_id": "X",
        "base_unit": "unit",
        "units": [],
    },
    {"service_id": "Y", "base_unit": "unit", "units": []},
    {"service_id": "Z", "base_unit": "unit", "units": []},
    {"service_id": "W", "base_unit": "unit", "units": []},
]


def line(site, service, min_cents, max_cents, *, quantity=1, unit=None):
    if unit is None:
        unit = next(
            row["base_unit"] for row in SERVICES if row["service_id"] == service
        )
    return {
        "site_id": site,
        "service_id": service,
        "quantity": quantity,
        "unit": unit,
        "min_cents": min_cents,
        "max_cents": max_cents,
    }


def site_totals(rows):
    return [{"site_id": site, "total_cents": total} for site, total in rows]


def service_totals(rows):
    return [
        {"service_id": service, "total_cents": total} for service, total in rows
    ]


# --- Consistent annexes for lot LOT-1 (price 1,500 cents) -----------------

# O1: flexible annex.  The basket (X-site Q, Y-site P) can total anywhere
# from 400 to 700 cents depending on the complete allocation.
O1_LINES = [
    line("X", "P", 100, 400),
    line("X", "Q", 50, 300, quantity=2, unit="block"),
    line("X", "R", 0, 300, quantity=1, unit="half_day"),
    line("Y", "P", 100, 400, quantity=16, unit="hour"),
    line("Y", "Q", 50, 300),
    line("Y", "R", 0, 300),
]
O1 = {
    "lot_id": "LOT-1",
    "offer_id": "O1",
    "site_totals": site_totals([("X", 800), ("Y", 700)]),
    "service_totals": service_totals([("P", 600), ("Q", 500), ("R", 400)]),
    "lines": O1_LINES,
}

# O2: same annex shape, but the two basket lines are fixed, so the basket
# always totals exactly 500 cents.
O2 = {
    "lot_id": "LOT-1",
    "offer_id": "O2",
    "site_totals": site_totals([("X", 800), ("Y", 700)]),
    "service_totals": service_totals([("P", 600), ("Q", 500), ("R", 400)]),
    "lines": [
        line("X", "P", 150, 350),
        line("X", "Q", 200, 200, quantity=2, unit="block"),
        line("X", "R", 100, 450, quantity=1, unit="half_day"),
        line("Y", "P", 300, 300, quantity=16, unit="hour"),
        line("Y", "Q", 100, 400),
        line("Y", "R", 50, 350),
    ],
}

# O4: strictly cheaper than O1 on this basket (its highest possible basket
# total, 300, is below O1's lowest, 400).
O4 = {
    "lot_id": "LOT-1",
    "offer_id": "O4",
    "site_totals": site_totals([("X", 800), ("Y", 700)]),
    "service_totals": service_totals([("P", 600), ("Q", 500), ("R", 400)]),
    "lines": [
        line("X", "P", 200, 500),
        line("X", "Q", 100, 100, quantity=2, unit="block"),
        line("X", "R", 50, 500, quantity=1, unit="half_day"),
        line("Y", "P", 50, 250, quantity=16, unit="hour"),
        line("Y", "Q", 150, 450),
        line("Y", "R", 100, 500),
    ],
}

# O5: self-consistent annex that does not cover the Y site at all, so it
# cannot join the basket comparison even though every amount balances.
O5_LINES = [
    line("X", "P", 0, 200),
    # One block equals six base visits; the basket selects two blocks
    # (12 base visits), so this line also demonstrates a quantity mismatch.
    line("X", "Q", 0, 200, quantity=1, unit="block"),
]
O5 = {
    "lot_id": "LOT-1",
    "offer_id": "O5",
    "site_totals": site_totals([("X", 200)]),
    "service_totals": service_totals([("P", 90), ("Q", 110)]),
    "lines": O5_LINES,
}
# Rebuild the service subtotals from a real allocation so O5 stays
# self-consistent regardless of the placeholder values above.
_ok, _o5_alloc, _o5_conflict = solve_transportation(
    {
        "site_totals": {"X": 200},
        "service_totals": {"P": 90, "Q": 110},
        "lines": O5_LINES,
    },
    set(),
    "feasible",
)
assert _ok and _o5_conflict is None
_o5_service_sums = {}
for (_site, service), amount in _o5_alloc.items():
    _o5_service_sums[service] = _o5_service_sums.get(service, 0) + amount
O5["service_totals"] = service_totals(sorted(_o5_service_sums.items()))

# --- Inconsistent annex: subtotals match the price but cannot coexist -----

# Sites A, B only provide X and Y (8 cents together) while categories X and Y
# only have 6 cents of subtotal between them.  Every line allows 0..12.
O3_LINES = []
for site, cats in (
    ("A", ("X", "Y")),
    ("B", ("X", "Y")),
    ("C", ("X", "Y", "Z", "W")),
    ("D", ("X", "Y", "Z", "W")),
):
    for service in cats:
        O3_LINES.append(line(site, service, 0, 12))
O3 = {
    "lot_id": "LOT-1",
    "offer_id": "O3",
    "site_totals": site_totals(
        [("A", 4), ("B", 4), ("C", 2), ("D", 2)]
    ),
    "service_totals": service_totals(
        [("X", 3), ("Y", 3), ("Z", 3), ("W", 3)]
    ),
    "lines": O3_LINES,
}

# --- Separate lot LOT-2 for the boundary-touch case ------------------------

# Both annexes total 200 cents over sites X, Y and services P, Q.
TOUCH_O4 = {
    "lot_id": "LOT-2",
    "offer_id": "O7",
    "site_totals": site_totals([("X", 100), ("Y", 100)]),
    "service_totals": service_totals([("P", 100), ("Q", 100)]),
    "lines": [
        line("X", "P", 40, 80),
        line("X", "Q", 20, 60),
        line("Y", "P", 20, 60),
        line("Y", "Q", 40, 80),
    ],
}
TOUCH_O6 = {
    "lot_id": "LOT-2",
    "offer_id": "O6",
    "site_totals": site_totals([("X", 100), ("Y", 100)]),
    "service_totals": service_totals([("P", 100), ("Q", 100)]),
    "lines": [
        line("X", "P", 40, 40),
        line("X", "Q", 60, 60),
        line("Y", "P", 60, 60),
        line("Y", "Q", 40, 40),
    ],
}

REVIEW = {
    "schema_version": 1,
    "lots": [
        {
            "lot_id": "LOT-1",
            "offers": [
                {
                    "offer_id": "O1",
                    "vendor_id": "V1",
                    "price_cents": 1500,
                    "eligible": True,
                    "reasons": [],
                    "awarded": True,
                    "selection_reasons": [],
                },
                {
                    "offer_id": "O2",
                    "vendor_id": "V2",
                    "price_cents": 1500,
                    "eligible": True,
                    "reasons": [],
                    "awarded": False,
                    "selection_reasons": ["objective_tradeoff"],
                },
                {
                    "offer_id": "O3",
                    "vendor_id": "V3",
                    "price_cents": 12,
                    "eligible": False,
                    "reasons": ["over_budget"],
                    "awarded": False,
                    "selection_reasons": [],
                },
                {
                    "offer_id": "O4",
                    "vendor_id": "V4",
                    "price_cents": 1500,
                    "eligible": True,
                    "reasons": [],
                    "awarded": False,
                    "selection_reasons": ["objective_tradeoff"],
                },
                {
                    "offer_id": "O5",
                    "vendor_id": "V5",
                    "price_cents": 200,
                    "eligible": True,
                    "reasons": [],
                    "awarded": False,
                    "selection_reasons": ["objective_tradeoff"],
                },
            ],
            "award_offer_id": "O1",
            "award_vendor_id": "V1",
            "awarded": True,
        },
        {
            "lot_id": "LOT-2",
            "offers": [
                {
                    "offer_id": "O7",
                    "vendor_id": "V4",
                    "price_cents": 200,
                    "eligible": True,
                    "reasons": [],
                    "awarded": False,
                    "selection_reasons": ["objective_tradeoff"],
                },
                {
                    "offer_id": "O6",
                    "vendor_id": "V6",
                    "price_cents": 200,
                    "eligible": True,
                    "reasons": [],
                    "awarded": True,
                    "selection_reasons": [],
                },
            ],
            "award_offer_id": "O6",
            "award_vendor_id": "V6",
            "awarded": True,
        },
    ],
    "awarded": 2,
    "package": {
        "budget_cents": 100000,
        "awarded_total_cents": 1700,
        "remaining_cents": 98300,
        "lots_total": 2,
        "lots_awarded": 2,
        "vendors": [],
    },
}

ANNEX = {
    "services": SERVICES,
    "quotes": [O1, O2, O3, O4, O5, TOUCH_O4, TOUCH_O6],
}

# Basket on LOT-1: two lines expressed in different units that convert to the
# same base quantities across O1, O2 and O4 (12 base visits and 16 base
# hours).  O3 lacks both lines; O5 lacks the Y line.
BASKET = {
    "lot_id": "LOT-1",
    "offer_ids": ["O1", "O2", "O3", "O4", "O5"],
    "items": [
        {"site_id": "X", "service_id": "Q", "quantity": 2, "unit": "block"},
        {"site_id": "Y", "service_id": "P", "quantity": 16, "unit": "hour"},
    ],
}

# Boundary-touch basket on LOT-2: O7 can reach up to 60 cents, O6 is fixed at
# 60 cents, so the intervals merely meet and no strict claim is allowed.
TOUCH_BASKET = {
    "lot_id": "LOT-2",
    "offer_ids": ["O7", "O6"],
    "items": [{"site_id": "X", "service_id": "Q", "quantity": 1, "unit": "visit"}],
}


def write_json(path, payload):
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main():
    result = compare_bid_costs(REVIEW, ANNEX, BASKET)
    touch_result = compare_bid_costs(REVIEW, ANNEX, TOUCH_BASKET)

    # Every emitted allocation must independently re-verify against its
    # annex; the generator fails rather than writing a wrong fixture.
    quote_by_key = {(q["lot_id"], q["offer_id"]): q for q in ANNEX["quotes"]}
    for offer in result["offers"]:
        quote = quote_by_key[(offer["lot_id"], offer["offer_id"])]
        expected = offer["reviewed_price_cents"]
        for field in ("sample_allocation", "min_allocation", "max_allocation"):
            allocation = offer[field]
            if allocation is not None:
                report = verify_allocation(quote, allocation, expected)
                assert report["valid"], report
    for offer in touch_result["offers"]:
        quote = quote_by_key[(offer["lot_id"], offer["offer_id"])]
        for field in ("sample_allocation", "min_allocation", "max_allocation"):
            allocation = offer[field]
            if allocation is not None:
                assert verify_allocation(
                    quote, allocation, offer["reviewed_price_cents"]
                )["valid"]

    write_json(HERE / "review.json", REVIEW)
    write_json(HERE / "annex.json", ANNEX)
    write_json(HERE / "basket.json", BASKET)
    write_json(HERE / "touch-basket.json", TOUCH_BASKET)
    write_json(HERE / "expected-result.json", result)
    write_json(HERE / "expected-touch-result.json", touch_result)
    print("wrote 6 deterministic cost-compare fixtures to", HERE)


if __name__ == "__main__":
    main()
