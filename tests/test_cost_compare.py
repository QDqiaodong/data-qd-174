import copy
import json
import random
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from procurement.costcompare import (
    CostCompareError,
    compare_bid_costs,
    load_json_file,
    parse_json_text,
    verify_allocation,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "cost-compare"
REPO = Path(__file__).resolve().parent.parent


def load(name):
    return load_json_file(FIXTURES / name)


def line(site, service, lo, hi, *, quantity=1, unit="u"):
    return {
        "site_id": site,
        "service_id": service,
        "quantity": quantity,
        "unit": unit,
        "min_cents": lo,
        "max_cents": hi,
    }


def quote(offer_id, site_totals, service_totals, lines, lot_id="L1"):
    return {
        "lot_id": lot_id,
        "offer_id": offer_id,
        "site_totals": [
            {"site_id": site, "total_cents": total}
            for site, total in site_totals.items()
        ],
        "service_totals": [
            {"service_id": service, "total_cents": total}
            for service, total in service_totals.items()
        ],
        "lines": lines,
    }


def simple_services(*service_ids):
    return [
        {"service_id": service_id, "base_unit": "u", "units": []}
        for service_id in service_ids
    ]


def review(offers, lot_id="L1"):
    return {
        "lots": [
            {
                "lot_id": lot_id,
                "offers": [
                    {"offer_id": offer_id, "price_cents": price}
                    for offer_id, price in offers
                ],
            }
        ]
    }


def result_offer(result, offer_id):
    return next(row for row in result["offers"] if row["offer_id"] == offer_id)


def allocation_map(rows):
    return {
        (row["site_id"], row["service_id"]): row["amount_cents"] for row in rows
    }


class FixtureEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = load("review.json")
        cls.annex = load("annex.json")
        cls.basket = load("basket.json")
        cls.expected = load("expected-result.json")
        cls.touch_basket = load("touch-basket.json")
        cls.touch_expected = load("expected-touch-result.json")

    def test_fixture_matches_expected_output(self):
        result = compare_bid_costs(self.review, self.annex, self.basket)
        self.assertEqual(result, self.expected)

    def test_every_emitted_allocation_independently_verifies(self):
        for offer in self.expected["offers"]:
            raw_quote = next(
                q
                for q in self.annex["quotes"]
                if q["lot_id"] == offer["lot_id"]
                and q["offer_id"] == offer["offer_id"]
            )
            for field in ("sample_allocation", "min_allocation", "max_allocation"):
                rows = offer[field]
                if rows is None:
                    continue
                report = verify_allocation(
                    raw_quote, rows, offer["reviewed_price_cents"]
                )
                self.assertTrue(report["valid"], report)
                self.assertEqual(
                    report["allocated_total_cents"], offer["reviewed_price_cents"]
                )

    def test_annex_conclusions_and_participation(self):
        by_offer = {row["offer_id"]: row for row in self.expected["offers"]}
        for offer_id in ("O1", "O2", "O4"):
            self.assertTrue(by_offer[offer_id]["annex_self_consistent"])
            self.assertTrue(by_offer[offer_id]["participates_in_comparison"])
        self.assertFalse(by_offer["O3"]["annex_self_consistent"])
        self.assertFalse(by_offer["O3"]["participates_in_comparison"])
        self.assertFalse(by_offer["O5"]["participates_in_comparison"])
        self.assertTrue(by_offer["O5"]["annex_self_consistent"])
        self.assertIsNotNone(by_offer["O5"]["sample_allocation"])

    def test_extrema_are_reached_by_complete_allocations(self):
        o1 = result_offer(self.expected, "O1")
        self.assertEqual(o1["basket_min_cents"], 400)
        self.assertEqual(o1["basket_max_cents"], 700)
        for field, expected in (("min_allocation", 400), ("max_allocation", 700)):
            rows = o1[field]
            # The allocation covers every annex line, including unselected ones.
            self.assertEqual(len(rows), 6)
            basket_total = sum(
                row["amount_cents"]
                for row in rows
                if (row["site_id"], row["service_id"])
                in {("X", "Q"), ("Y", "P")}
            )
            self.assertEqual(basket_total, expected)

    def test_strict_cheaper_verdict(self):
        verdicts = {
            tuple(v["offer_ids"]): v for v in self.expected["pairwise_verdicts"]
        }
        self.assertEqual(verdicts[("O1", "O4")]["verdict"], "strictly_cheaper")
        self.assertEqual(verdicts[("O1", "O4")]["cheaper_offer_id"], "O4")
        self.assertEqual(verdicts[("O2", "O4")]["cheaper_offer_id"], "O4")
        self.assertEqual(verdicts[("O1", "O2")]["verdict"], "indeterminate")

    def test_one_bad_annex_keeps_other_comparisons(self):
        # O3 is inconsistent and O5 is excluded, but O1/O2/O4 still compare.
        self.assertEqual(len(self.expected["pairwise_verdicts"]), 3)
        participants = {
            offer_id
            for verdict in self.expected["pairwise_verdicts"]
            for offer_id in verdict["offer_ids"]
        }
        self.assertEqual(participants, {"O1", "O2", "O4"})

    def test_boundary_touch_is_not_a_strict_claim(self):
        result = compare_bid_costs(self.review, self.annex, self.touch_basket)
        self.assertEqual(result, self.touch_expected)
        verdict = result["pairwise_verdicts"][0]
        self.assertEqual(verdict["verdict"], "indeterminate")
        self.assertIsNone(verdict["cheaper_offer_id"])
        self.assertIn("boundary", verdict["note"])
        o6 = result_offer(result, "O6")
        o7 = result_offer(result, "O7")
        self.assertEqual((o6["basket_min_cents"], o6["basket_max_cents"]), (60, 60))
        self.assertEqual((o7["basket_min_cents"], o7["basket_max_cents"]), (20, 60))


class SpecWorkedExampleTests(unittest.TestCase):
    def test_fifteen_cent_annex_basket_ranges(self):
        # The exact example from the specification: total 15, sites X=8 Y=7,
        # categories P=10 Q=5, four lines each allowed 0..15.
        services = simple_services("P", "Q")
        lines = [
            line("X", "P", 0, 15),
            line("X", "Q", 0, 15),
            line("Y", "P", 0, 15),
            line("Y", "Q", 0, 15),
        ]
        annex = {
            "services": services,
            "quotes": [
                quote("O1", {"X": 8, "Y": 7}, {"P": 10, "Q": 5}, lines),
                quote(
                    "O2",
                    {"X": 8, "Y": 7},
                    {"P": 9, "Q": 6},
                    [
                        line("X", "P", 5, 5),
                        line("X", "Q", 3, 3),
                        line("Y", "P", 4, 4),
                        line("Y", "Q", 3, 3),
                    ],
                ),
            ],
        }
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "X", "service_id": "Q", "quantity": 1, "unit": "u"},
                {"site_id": "Y", "service_id": "P", "quantity": 1, "unit": "u"},
            ],
        }
        result = compare_bid_costs(review([("O1", 15), ("O2", 15)]), annex, basket)
        o1 = result_offer(result, "O1")
        self.assertTrue(o1["annex_self_consistent"])
        self.assertEqual(o1["basket_min_cents"], 2)
        self.assertEqual(o1["basket_max_cents"], 12)
        for field, expected in (("min_allocation", 2), ("max_allocation", 12)):
            rows = o1[field]
            report = verify_allocation(annex["quotes"][0], rows, 15)
            self.assertTrue(report["valid"], report)
            basket_total = sum(
                row["amount_cents"]
                for row in rows
                if (row["site_id"], row["service_id"])
                in {("X", "Q"), ("Y", "P")}
            )
            self.assertEqual(basket_total, expected)
        # O2's basket is fixed at 7; an allocation of O1 happening to total 2
        # must not be misread as O1 being necessarily cheaper.
        o2 = result_offer(result, "O2")
        self.assertEqual((o2["basket_min_cents"], o2["basket_max_cents"]), (7, 7))
        self.assertEqual(
            result["pairwise_verdicts"][0]["verdict"], "indeterminate"
        )

        # Selecting Q at both sites instead: that category subtotal is fixed
        # at 5, so the basket total is always 5.
        basket_q = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "X", "service_id": "Q", "quantity": 1, "unit": "u"},
                {"site_id": "Y", "service_id": "Q", "quantity": 1, "unit": "u"},
            ],
        }
        result_q = compare_bid_costs(
            review([("O1", 15), ("O2", 15)]), annex, basket_q
        )
        self.assertEqual(result_offer(result_q, "O1")["basket_min_cents"], 5)
        self.assertEqual(result_offer(result_q, "O1")["basket_max_cents"], 5)

    def test_crowded_sites_conflict_certificate(self):
        # The specification's second example: A,B (8 cents) cannot fit into
        # categories X,Y (6 cents).
        lines = []
        for site, cats in (
            ("A", ("X", "Y")),
            ("B", ("X", "Y")),
            ("C", ("X", "Y", "Z", "W")),
            ("D", ("X", "Y", "Z", "W")),
        ):
            for service in cats:
                lines.append(line(site, service, 0, 12))
        annex = {
            "services": simple_services("X", "Y", "Z", "W"),
            "quotes": [
                quote(
                    "O3",
                    {"A": 4, "B": 4, "C": 2, "D": 2},
                    {"X": 3, "Y": 3, "Z": 3, "W": 3},
                    lines,
                )
            ],
        }
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O3"],
            "items": [
                {"site_id": "A", "service_id": "X", "quantity": 1, "unit": "u"}
            ],
        }
        # Two different offers are required; add a second consistent quote.
        annex["quotes"].append(
            quote(
                "O9",
                {"A": 4, "B": 4, "C": 2, "D": 2},
                {"X": 3, "Y": 3, "Z": 3, "W": 3},
                [
                    line(site, service, 0, 12)
                    for site, cats in (
                        ("A", ("X", "Y")),
                        ("B", ("X", "Y")),
                        ("C", ("X", "Y", "Z", "W")),
                        ("D", ("X", "Y", "Z", "W")),
                    )
                    for service in cats
                ],
            )
        )
        # O9 cannot balance either with the same subtotals; instead give it
        # a tiny feasible annex covering the basket line.
        annex["quotes"][1] = quote(
            "O9", {"A": 5}, {"X": 5}, [line("A", "X", 0, 12)]
        )
        basket["offer_ids"] = ["O3", "O9"]
        result = compare_bid_costs(review([("O3", 12), ("O9", 5)]), annex, basket)
        o3 = result_offer(result, "O3")
        self.assertFalse(o3["annex_self_consistent"])
        conflict = o3["conflict"]
        self.assertEqual(conflict["site_ids"], ["A", "B"])
        self.assertEqual(conflict["service_ids"], ["X", "Y"])
        self.assertEqual(conflict["subtotal_difference_cents"], 2)
        self.assertEqual(conflict["bound_cents"], 0)
        self.assertGreater(
            conflict["subtotal_difference_cents"], conflict["bound_cents"]
        )
        # The bound must be reproducible from the listed line bounds alone.
        upper = sum(row["max_cents"] for row in conflict["upper_bound_lines"])
        lower = sum(row["min_cents"] for row in conflict["lower_bound_lines"])
        self.assertEqual(upper - lower, conflict["bound_cents"])
        # The other, consistent offer still produces its standalone finding.
        o9 = result_offer(result, "O9")
        self.assertTrue(o9["annex_self_consistent"])


class UnitConversionTests(unittest.TestCase):
    def _annex(self):
        services = [
            {
                "service_id": "P",
                "base_unit": "hour",
                "units": [
                    {"unit": "day", "numerator": 8, "denominator": 1},
                    {"unit": "shift", "numerator": 4, "denominator": 1},
                ],
            }
        ]
        return {
            "services": services,
            "quotes": [
                # Two days = 16 base hours, expressed in the annex as four
                # shifts = 16 base hours.
                quote(
                    "O1",
                    {"X": 100},
                    {"P": 100},
                    [line("X", "P", 0, 100, quantity=4, unit="shift")],
                ),
                # 7 shifts = 28 base hours: close to but not equal to 16.
                quote(
                    "O2",
                    {"X": 100},
                    {"P": 100},
                    [line("X", "P", 0, 100, quantity=7, unit="shift")],
                ),
            ],
        }

    def test_different_units_converting_to_equal_quantity_compare(self):
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "X", "service_id": "P", "quantity": 2, "unit": "day"}
            ],
        }
        result = compare_bid_costs(
            review([("O1", 100), ("O2", 100)]), self._annex(), basket
        )
        o1 = result_offer(result, "O1")
        self.assertTrue(o1["basket_match"]["matches"])
        self.assertTrue(o1["participates_in_comparison"])
        o2 = result_offer(result, "O2")
        self.assertFalse(o2["basket_match"]["matches"])
        mismatch = o2["basket_match"]["quantity_mismatches"][0]
        self.assertEqual(mismatch["basket"]["base_quantity"], 16)
        self.assertEqual(mismatch["annex"]["base_quantity"], 28)
        self.assertFalse(o2["participates_in_comparison"])
        self.assertIn("basket_quantity_mismatch", o2["exclusion_reasons"])
        # With one participant no pair can be formed; its interval survives.
        self.assertEqual(result["pairwise_verdicts"], [])
        self.assertEqual(o1["basket_min_cents"], 100)

    def test_fractional_quantities_use_exact_canonical_form(self):
        services = [
            {
                "service_id": "P",
                "base_unit": "piece",
                "units": [
                    {"unit": "half", "numerator": 1, "denominator": 2},
                    {"unit": "pack", "numerator": 5, "denominator": 3},
                ],
            }
        ]
        annex = {
            "services": services,
            "quotes": [
                quote(
                    "O1",
                    {"X": 10},
                    {"P": 10},
                    [line("X", "P", 0, 10, quantity=7, unit="pack")],
                ),
                quote(
                    "O2",
                    {"X": 10},
                    {"P": 10},
                    [line("X", "P", 0, 10, quantity=3, unit="half")],
                ),
            ],
        }
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "X", "service_id": "P", "quantity": 3, "unit": "half"}
            ],
        }
        result = compare_bid_costs(
            review([("O1", 10), ("O2", 10)]), annex, basket
        )
        self.assertEqual(
            result["basket"]["items"][0]["base_quantity"],
            {"numerator": 3, "denominator": 2},
        )
        o1 = result_offer(result, "O1")
        mismatch = o1["basket_match"]["quantity_mismatches"][0]
        # 7 packs = 35/3 base pieces, never rounded to compare with 3/2.
        self.assertEqual(
            mismatch["annex"]["base_quantity"],
            {"numerator": 35, "denominator": 3},
        )
        self.assertNotEqual(Fraction(35, 3), Fraction(3, 2))
        o2 = result_offer(result, "O2")
        self.assertTrue(o2["basket_match"]["matches"])


class LowerBoundTests(unittest.TestCase):
    def test_positive_lower_bounds_shrink_the_range(self):
        services = simple_services("P", "Q")
        lines = [
            line("X", "P", 40, 90),
            line("X", "Q", 10, 60),
            line("Y", "P", 10, 60),
            line("Y", "Q", 40, 90),
        ]
        annex = {
            "services": services,
            "quotes": [
                quote("O1", {"X": 100, "Y": 100}, {"P": 100, "Q": 100}, lines)
            ],
        }
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "X", "service_id": "Q", "quantity": 1, "unit": "u"}
            ],
        }
        annex["quotes"].append(
            quote(
                "O2",
                {"X": 100, "Y": 100},
                {"P": 100, "Q": 100},
                [
                    line("X", "P", 0, 100),
                    line("X", "Q", 0, 100),
                    line("Y", "P", 0, 100),
                    line("Y", "Q", 0, 100),
                ],
            )
        )
        result = compare_bid_costs(
            review([("O1", 200), ("O2", 200)]), annex, basket
        )
        o1 = result_offer(result, "O1")
        # X/Q's own bounds are [10, 60]; the positive lower bounds elsewhere
        # make both extremes attainable while ruling out 0 (which the
        # zero-bounded O2 below can reach).
        self.assertEqual(o1["basket_min_cents"], 10)
        self.assertEqual(o1["basket_max_cents"], 60)
        o2 = result_offer(result, "O2")
        self.assertEqual(o2["basket_min_cents"], 0)
        self.assertEqual(o2["basket_max_cents"], 100)

    def test_positive_lower_bounds_participate_in_conflict_proof(self):
        # Category Q must total only 5 cents, but the X/Q line alone carries
        # a positive lower bound of 10 cents.  The resulting certificate uses
        # that lower bound on the not-S x T lines: difference 0 > bound -10.
        services = simple_services("P", "Q")
        lines = [
            line("X", "P", 40, 60),
            line("X", "Q", 10, 40),
            line("Y", "P", 10, 40),
            line("Y", "Q", 0, 40),
        ]
        annex = {
            "services": services,
            "quotes": [
                quote(
                    "O1",
                    {"X": 100, "Y": 0},
                    {"P": 95, "Q": 5},
                    lines,
                ),
                quote("O2", {"Z": 5}, {"Q": 5}, [line("Z", "Q", 0, 80)]),
            ],
        }
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "Z", "service_id": "Q", "quantity": 1, "unit": "u"}
            ],
        }
        result = compare_bid_costs(
            review([("O1", 100), ("O2", 5)]), annex, basket
        )
        o1 = result_offer(result, "O1")
        self.assertFalse(o1["annex_self_consistent"])
        conflict = o1["conflict"]
        self.assertGreater(
            conflict["subtotal_difference_cents"], conflict["bound_cents"]
        )
        # The strictness comes from a positive lower bound, not merely from
        # zero-bounded lines.
        self.assertTrue(
            any(row["min_cents"] > 0 for row in conflict["lower_bound_lines"])
        )
        # Re-derive every number in the certificate from raw inputs.
        site_set = set(conflict["site_ids"])
        service_set = set(conflict["service_ids"])
        expected_diff = sum(
            row["total_cents"]
            for row in annex["quotes"][0]["site_totals"]
            if row["site_id"] in site_set
        ) - sum(
            row["total_cents"]
            for row in annex["quotes"][0]["service_totals"]
            if row["service_id"] in service_set
        )
        expected_bound = sum(
            row["max_cents"]
            for row in lines
            if row["site_id"] in site_set and row["service_id"] not in service_set
        ) - sum(
            row["min_cents"]
            for row in lines
            if row["site_id"] not in site_set and row["service_id"] in service_set
        )
        self.assertEqual(
            conflict["subtotal_difference_cents"], expected_diff
        )
        self.assertEqual(conflict["bound_cents"], expected_bound)
        # O2 remains usable despite O1 being inconsistent.
        o2 = result_offer(result, "O2")
        self.assertTrue(o2["annex_self_consistent"])
        self.assertTrue(o2["participates_in_comparison"])


class NecessaryEqualityTests(unittest.TestCase):
    def test_two_single_value_intervals_are_necessarily_equal(self):
        services = simple_services("P")
        annex = {
            "services": services,
            "quotes": [
                quote("O1", {"X": 50}, {"P": 50}, [line("X", "P", 50, 50)]),
                quote("O2", {"X": 50}, {"P": 50}, [line("X", "P", 20, 80)]),
            ],
        }
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "X", "service_id": "P", "quantity": 1, "unit": "u"}
            ],
        }
        result = compare_bid_costs(
            review([("O1", 50), ("O2", 50)]), annex, basket
        )
        verdict = result["pairwise_verdicts"][0]
        self.assertEqual(verdict["verdict"], "necessarily_equal")
        self.assertIsNone(verdict["cheaper_offer_id"])
        self.assertEqual(result_offer(result, "O2")["basket_min_cents"], 50)
        self.assertEqual(result_offer(result, "O2")["basket_max_cents"], 50)


class TotalMismatchTests(unittest.TestCase):
    def test_each_subtotal_set_mismatch_is_named(self):
        services = simple_services("P")
        good = quote("O1", {"X": 60}, {"P": 60}, [line("X", "P", 0, 100)])
        bad_site = quote(
            "O2", {"X": 70}, {"P": 60}, [line("X", "P", 0, 100)]
        )
        bad_service = quote(
            "O3", {"X": 60}, {"P": 55}, [line("X", "P", 0, 100)]
        )
        annex = {"services": services, "quotes": [good, bad_site, bad_service]}
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2", "O3"],
            "items": [
                {"site_id": "X", "service_id": "P", "quantity": 1, "unit": "u"}
            ],
        }
        result = compare_bid_costs(
            review([("O1", 60), ("O2", 60), ("O3", 60)]), annex, basket
        )
        o2 = result_offer(result, "O2")
        self.assertEqual(
            o2["total_mismatches"],
            [
                {
                    "subtotals": "site_totals",
                    "actual_total_cents": 70,
                    "expected_total_cents": 60,
                }
            ],
        )
        self.assertFalse(o2["annex_self_consistent"])
        o3 = result_offer(result, "O3")
        self.assertEqual(
            o3["total_mismatches"][0]["subtotals"], "service_totals"
        )
        self.assertEqual(o3["total_mismatches"][0]["actual_total_cents"], 55)
        # The good quote still participates alone.
        self.assertTrue(result_offer(result, "O1")["participates_in_comparison"])
        self.assertEqual(result["pairwise_verdicts"], [])


class MissingBasketLineTests(unittest.TestCase):
    def test_missing_line_keeps_sample_allocation_and_reason(self):
        services = simple_services("P", "Q")
        annex = {
            "services": services,
            "quotes": [
                quote(
                    "O1",
                    {"X": 100},
                    {"P": 60, "Q": 40},
                    [line("X", "P", 0, 100), line("X", "Q", 0, 100)],
                ),
                quote(
                    "O2",
                    {"X": 50, "Y": 50},
                    {"P": 60, "Q": 40},
                    [
                        line("X", "P", 0, 100),
                        line("X", "Q", 0, 100),
                        line("Y", "P", 0, 100),
                        line("Y", "Q", 0, 100),
                    ],
                ),
            ],
        }
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "Y", "service_id": "P", "quantity": 1, "unit": "u"},
                {"site_id": "Y", "service_id": "Q", "quantity": 1, "unit": "u"},
            ],
        }
        result = compare_bid_costs(
            review([("O1", 100), ("O2", 100)]), annex, basket
        )
        o1 = result_offer(result, "O1")
        self.assertTrue(o1["annex_self_consistent"])
        self.assertIsNotNone(o1["sample_allocation"])
        self.assertEqual(
            o1["basket_match"]["missing_lines"],
            [
                {"site_id": "Y", "service_id": "P", "reason": "no_annex_line"},
                {"site_id": "Y", "service_id": "Q", "reason": "no_annex_line"},
            ],
        )
        self.assertFalse(o1["participates_in_comparison"])
        self.assertIn("basket_item_missing_from_annex", o1["exclusion_reasons"])
        # Unselected lines must never be subtracted or scaled away: O2's
        # complete allocation still totals the full price.
        o2 = result_offer(result, "O2")
        self.assertTrue(o2["participates_in_comparison"])
        self.assertEqual(
            sum(row["amount_cents"] for row in o2["min_allocation"]), 100
        )


class DeterminismTests(unittest.TestCase):
    def test_array_reordering_produces_identical_output(self):
        review_doc = load("review.json")
        annex = load("annex.json")
        basket = load("basket.json")
        baseline = json.dumps(
            compare_bid_costs(review_doc, annex, basket), sort_keys=True
        )
        rng = random.Random(4242)
        for _ in range(5):
            shuffled_annex = copy.deepcopy(annex)
            rng.shuffle(shuffled_annex["services"])
            rng.shuffle(shuffled_annex["quotes"])
            for quote_row in shuffled_annex["quotes"]:
                rng.shuffle(quote_row["lines"])
                rng.shuffle(quote_row["site_totals"])
                rng.shuffle(quote_row["service_totals"])
            for service in shuffled_annex["services"]:
                rng.shuffle(service["units"])
            shuffled_basket = copy.deepcopy(basket)
            rng.shuffle(shuffled_basket["offer_ids"])
            rng.shuffle(shuffled_basket["items"])
            shuffled_review = copy.deepcopy(review_doc)
            rng.shuffle(shuffled_review["lots"])
            for lot in shuffled_review["lots"]:
                rng.shuffle(lot["offers"])
            again = json.dumps(
                compare_bid_costs(
                    shuffled_review, shuffled_annex, shuffled_basket
                ),
                sort_keys=True,
            )
            self.assertEqual(again, baseline)


class LargeAmountTests(unittest.TestCase):
    def test_large_totals_without_per_cent_enumeration(self):
        huge = 10**15
        services = simple_services("P", "Q", "R")
        lines = []
        for site in ("X", "Y", "Z"):
            for service in ("P", "Q", "R"):
                lines.append(line(site, service, huge // 100, huge))
        site_totals = {site: huge for site in ("X", "Y", "Z")}
        service_totals = {service: huge for service in ("P", "Q", "R")}
        annex = {
            "services": services,
            "quotes": [
                quote("O1", site_totals, service_totals, lines),
                quote(
                    "O2",
                    site_totals,
                    service_totals,
                    [dict(row) for row in lines],
                ),
            ],
        }
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "X", "service_id": "Q", "quantity": 1, "unit": "u"}
            ],
        }
        result = compare_bid_costs(
            review([("O1", 3 * huge), ("O2", 3 * huge)]), annex, basket
        )
        for offer_id in ("O1", "O2"):
            offer = result_offer(result, offer_id)
            self.assertTrue(offer["annex_self_consistent"])
            for field in ("min_allocation", "max_allocation"):
                rows = offer[field]
                report = verify_allocation(
                    annex["quotes"][0], rows, 3 * huge
                )
                self.assertTrue(report["valid"], report)
            self.assertIsInstance(offer["basket_min_cents"], int)
            self.assertIsInstance(offer["basket_max_cents"], int)


class InputErrorTests(unittest.TestCase):
    def setUp(self):
        self.review_doc = review([("O1", 100), ("O2", 100)])
        self.annex = {
            "services": simple_services("P"),
            "quotes": [
                quote("O1", {"X": 100}, {"P": 100}, [line("X", "P", 0, 100)]),
                quote("O2", {"X": 100}, {"P": 100}, [line("X", "P", 0, 100)]),
            ],
        }
        self.basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O2"],
            "items": [
                {"site_id": "X", "service_id": "P", "quantity": 1, "unit": "u"}
            ],
        }

    def assert_input_error(self, regex, *, review_doc=None, annex=None, basket=None):
        with self.assertRaisesRegex(CostCompareError, regex):
            compare_bid_costs(
                self.review_doc if review_doc is None else review_doc,
                self.annex if annex is None else annex,
                self.basket if basket is None else basket,
            )

    def test_strict_json_loading(self):
        with self.assertRaisesRegex(CostCompareError, "duplicate JSON object key"):
            parse_json_text('{"a": 1, "a": 2}', "dup.json")
        with self.assertRaisesRegex(CostCompareError, "non-finite"):
            parse_json_text('{"a": NaN}', "nan.json")
        with self.assertRaisesRegex(CostCompareError, "non-finite"):
            parse_json_text('{"a": Infinity}', "inf.json")
        with self.assertRaisesRegex(CostCompareError, "invalid JSON"):
            parse_json_text("{not json", "broken.json")

    def test_booleans_are_not_integers(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["lines"][0]["quantity"] = True
        self.assert_input_error("positive integer", annex=annex)
        annex2 = copy.deepcopy(self.annex)
        annex2["quotes"][0]["site_totals"][0]["total_cents"] = False
        self.assert_input_error("non-negative integer", annex=annex2)

    def test_decimal_numbers_rejected(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["lines"][0]["min_cents"] = 1.5
        self.assert_input_error("integer", annex=annex)

    def test_empty_identifier(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["site_totals"][0]["site_id"] = ""
        self.assert_input_error("non-empty string", annex=annex)

    def test_inverted_range(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["lines"][0]["min_cents"] = 90
        annex["quotes"][0]["lines"][0]["max_cents"] = 10
        self.assert_input_error("min_cents", annex=annex)

    def test_duplicate_line_identity(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["lines"].append(line("X", "P", 0, 10))
        self.assert_input_error("listed more than once", annex=annex)

    def test_unknown_unit(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["lines"][0]["unit"] = "furlong"
        self.assert_input_error("unit", annex=annex)

    def test_invalid_conversion_coefficient(self):
        annex = copy.deepcopy(self.annex)
        annex["services"][0]["units"] = [
            {"unit": "day", "numerator": 0, "denominator": 1}
        ]
        self.assert_input_error("positive integer", annex=annex)
        annex["services"][0]["units"] = [
            {"unit": "day", "numerator": 8, "denominator": -2}
        ]
        self.assert_input_error("positive integer", annex=annex)

    def test_unknown_lot_or_offer_reference(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["offer_id"] = "GHOST"
        self.assert_input_error("unknown lot", annex=annex)
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["lot_id"] = "L9"
        self.assert_input_error("unknown lot", annex=annex)

    def test_selected_offer_without_annex(self):
        review_doc = {
            "lots": [
                {
                    "lot_id": "L1",
                    "offers": [
                        {"offer_id": "O1", "price_cents": 100},
                        {"offer_id": "O8", "price_cents": 100},
                    ],
                }
            ]
        }
        annex = {
            "services": simple_services("P"),
            "quotes": [
                quote("O1", {"X": 100}, {"P": 100}, [line("X", "P", 0, 100)])
            ],
        }
        basket = {
            "lot_id": "L1",
            "offer_ids": ["O1", "O8"],
            "items": [
                {"site_id": "X", "service_id": "P", "quantity": 1, "unit": "u"}
            ],
        }
        with self.assertRaisesRegex(CostCompareError, "no annex quote"):
            compare_bid_costs(review_doc, annex, basket)

    def test_basket_requires_two_distinct_offers(self):
        basket = copy.deepcopy(self.basket)
        basket["offer_ids"] = ["O1"]
        self.assert_input_error("at least two offers", basket=basket)
        basket = copy.deepcopy(self.basket)
        basket["offer_ids"] = ["O1", "O1"]
        self.assert_input_error("more than once", basket=basket)

    def test_basket_items_non_empty_and_unique(self):
        basket = copy.deepcopy(self.basket)
        basket["items"] = []
        self.assert_input_error("items must not be empty", basket=basket)
        basket = copy.deepcopy(self.basket)
        basket["items"].append(dict(basket["items"][0]))
        self.assert_input_error("selected more than once", basket=basket)

    def test_subtotals_must_cover_lines_exactly(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["site_totals"].append(
            {"site_id": "Z", "total_cents": 0}
        )
        self.assert_input_error("does not appear in any annex line", annex=annex)
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["site_totals"].pop()
        self.assert_input_error("cover exactly the lines", annex=annex)

    def test_duplicate_subtotal_entry(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"][0]["service_totals"].append(
            {"service_id": "P", "total_cents": 1}
        )
        self.assert_input_error("listed more than once", annex=annex)

    def test_duplicate_quote_identity(self):
        annex = copy.deepcopy(self.annex)
        annex["quotes"].append(copy.deepcopy(annex["quotes"][0]))
        self.assert_input_error("more than one quote", annex=annex)

    def test_duplicate_service_and_unit_declarations(self):
        annex = copy.deepcopy(self.annex)
        annex["services"].append(
            {"service_id": "P", "base_unit": "u", "units": []}
        )
        self.assert_input_error("declared more than once", annex=annex)
        annex = copy.deepcopy(self.annex)
        annex["services"][0]["units"] = [
            {"unit": "u", "numerator": 1, "denominator": 1}
        ]
        self.assert_input_error("duplicates the base unit", annex=annex)
        annex = copy.deepcopy(self.annex)
        annex["services"][0]["units"] = [
            {"unit": "d", "numerator": 2, "denominator": 1},
            {"unit": "d", "numerator": 3, "denominator": 1},
        ]
        self.assert_input_error("declared more than once", annex=annex)

    def test_duplicate_review_offer_id_rejected(self):
        review_doc = {
            "lots": [
                {"lot_id": "L1", "offers": [{"offer_id": "O1", "price_cents": 100}]},
                {"lot_id": "L2", "offers": [{"offer_id": "O1", "price_cents": 100}]},
            ]
        }
        with self.assertRaisesRegex(CostCompareError, "more than one lot"):
            compare_bid_costs(review_doc, self.annex, self.basket)

    def test_basket_unknown_service_and_unit(self):
        basket = copy.deepcopy(self.basket)
        basket["items"][0]["service_id"] = "NOPE"
        self.assert_input_error("not declared in annex.services", basket=basket)
        basket = copy.deepcopy(self.basket)
        basket["items"][0]["unit"] = "nonsense"
        self.assert_input_error("not declared for service", basket=basket)

    def test_basket_offer_from_other_lot_rejected(self):
        review_doc = {
            "lots": [
                {"lot_id": "L1", "offers": [{"offer_id": "O1", "price_cents": 100}]},
                {"lot_id": "L2", "offers": [{"offer_id": "O2", "price_cents": 100}]},
            ]
        }
        annex = {
            "services": simple_services("P"),
            "quotes": [
                quote("O1", {"X": 100}, {"P": 100}, [line("X", "P", 0, 100)], lot_id="L1"),
                quote("O2", {"X": 100}, {"P": 100}, [line("X", "P", 0, 100)], lot_id="L2"),
            ],
        }
        with self.assertRaisesRegex(CostCompareError, "not a reviewed offer of lot"):
            compare_bid_costs(review_doc, annex, self.basket)


class VerifyAllocationTests(unittest.TestCase):
    def test_reports_range_and_subtotal_violations(self):
        raw_quote = quote(
            "O1",
            {"X": 100},
            {"P": 100},
            [line("X", "P", 10, 40), line("X", "Q", 10, 80)],
        )
        raw_quote["service_totals"] = [
            {"service_id": "P", "total_cents": 50},
            {"service_id": "Q", "total_cents": 50},
        ]
        report = verify_allocation(
            raw_quote,
            [
                {"site_id": "X", "service_id": "P", "amount_cents": 5},
                {"site_id": "X", "service_id": "Q", "amount_cents": 95},
            ],
            100,
        )
        self.assertFalse(report["valid"])
        self.assertEqual(len(report["range_violations"]), 2)
        self.assertEqual(len(report["subtotal_mismatches"]), 2)

    def test_mapping_input_and_missing_line_rejected(self):
        raw_quote = quote("O1", {"X": 10}, {"P": 10}, [line("X", "P", 0, 10)])
        with self.assertRaisesRegex(CostCompareError, "missing annex lines"):
            verify_allocation(raw_quote, {})
        with self.assertRaisesRegex(CostCompareError, "absent from the annex"):
            verify_allocation(
                raw_quote,
                {("X", "P"): 5, ("Y", "P"): 5},
            )


class CliTests(unittest.TestCase):
    def run_cli(self, tmp, output_name="result.json"):
        # Invoke the CLI module by file path under -I: Python then puts the
        # package root on sys.path itself, with no site-package leakage.
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(REPO / "procurement" / "cost_compare_cli.py"),
                "--review",
                str(FIXTURES / "review.json"),
                "--annex",
                str(FIXTURES / "annex.json"),
                "--basket",
                str(FIXTURES / "basket.json"),
                "--output",
                str(Path(tmp) / output_name),
            ],
            capture_output=True,
            text=True,
            cwd=REPO,
        )

    def test_writes_result_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.run_cli(tmp)
            self.assertEqual(first.returncode, 0, first.stderr)
            summary = json.loads(first.stdout)
            self.assertEqual(summary["offers_checked"], 5)
            self.assertEqual(sorted(summary["offers_compared"]), ["O1", "O2", "O4"])
            output = Path(tmp) / "result.json"
            self.assertEqual(
                json.loads(output.read_text()),
                load("expected-result.json"),
            )
            second = self.run_cli(tmp)
            self.assertEqual(second.returncode, 2)
            self.assertIn("refusing to overwrite", second.stderr)

    def test_invalid_input_exits_2_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_annex = Path(tmp) / "annex.json"
            bad_annex.write_text("{broken")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(REPO / "procurement" / "cost_compare_cli.py"),
                    "--review",
                    str(FIXTURES / "review.json"),
                    "--annex",
                    str(bad_annex),
                    "--basket",
                    str(FIXTURES / "basket.json"),
                    "--output",
                    str(Path(tmp) / "out.json"),
                ],
                capture_output=True,
                text=True,
                cwd=REPO,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid JSON", result.stderr)
            self.assertFalse((Path(tmp) / "out.json").exists())


if __name__ == "__main__":
    unittest.main()
