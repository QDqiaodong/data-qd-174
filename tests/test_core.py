import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from procurement.core import review


def offer(offer_id, lot_id, vendor_id, price, *, submitted=None, documents=None):
    return {
        "id": offer_id,
        "lot_id": lot_id,
        "vendor_id": vendor_id,
        "submitted": submitted or "2026-03-01T10:00:00+00:00",
        "price_cents": price,
        "documents": ["doc"] if documents is None else documents,
    }


def lot(lot_id, *, start="2026-04-01", end="2026-05-01", budget=100000, documents=None):
    return {
        "id": lot_id,
        "deadline": "2026-03-10T17:00:00+00:00",
        "budget_cents": budget,
        "required_documents": ["doc"] if documents is None else documents,
        "service_start": start,
        "service_end": end,
    }


def vendor(vendor_id, capacity):
    return {"id": vendor_id, "concurrent_lot_capacity": capacity}


def payload(lots, offers, vendors, budget):
    return {
        "package_budget_cents": budget,
        "vendors": vendors,
        "lots": lots,
        "offers": offers,
    }


def by_lot(result, lot_id):
    return next(row for row in result["lots"] if row["lot_id"] == lot_id)


def offer_row(result, lot_id, offer_id):
    return next(
        row for row in by_lot(result, lot_id)["offers"] if row["offer_id"] == offer_id
    )


class PackageAwardTests(unittest.TestCase):
    def test_maximizes_awarded_lots_before_price(self):
        # Awarding L1 alone costs 1 cent, but L1+L2 costs 50 cents and awards
        # one more lot, so the lower total price must lose to the lot count.
        result = review(
            payload(
                [lot("L1"), lot("L2"), lot("L3")],
                [
                    offer("O1", "L1", "V1", 1),
                    offer("O2", "L2", "V2", 49),
                    offer("O3", "L3", "V3", 49),
                ],
                [vendor("V1", 1), vendor("V2", 1), vendor("V3", 1)],
                60,
            )
        )
        self.assertEqual(result["awarded"], 2)
        self.assertEqual(by_lot(result, "L1")["award_offer_id"], "O1")
        self.assertIsNone(by_lot(result, "L2")["award_offer_id"])
        self.assertEqual(by_lot(result, "L3")["award_offer_id"], "O3")
        self.assertEqual(result["package"]["awarded_total_cents"], 50)
        self.assertEqual(result["package"]["remaining_cents"], 10)

    def test_minimizes_total_price_for_equal_award_count(self):
        result = review(
            payload(
                [lot("L1"), lot("L2")],
                [
                    offer("O1", "L1", "V1", 70),
                    offer("O2", "L1", "V2", 10),
                    offer("O3", "L2", "V3", 20),
                    offer("O4", "L2", "V4", 80),
                ],
                [vendor("V1", 1), vendor("V2", 1), vendor("V3", 1), vendor("V4", 1)],
                100,
            )
        )
        self.assertEqual(result["awarded"], 2)
        self.assertEqual(by_lot(result, "L1")["award_offer_id"], "O2")
        self.assertEqual(by_lot(result, "L2")["award_offer_id"], "O3")
        self.assertEqual(result["package"]["awarded_total_cents"], 30)

    def test_tie_break_prefers_unawarded_earlier_lot(self):
        # Both choices award one lot for 100 cents; the sequence comparison
        # sorts an unawarded (empty) slot before an awarded offer ID, so L1
        # is left unawarded and L2 receives O2.
        result = review(
            payload(
                [lot("L1"), lot("L2")],
                [offer("O1", "L1", "V1", 100), offer("O2", "L2", "V2", 100)],
                [vendor("V1", 1), vendor("V2", 1)],
                100,
            )
        )
        self.assertEqual(result["awarded"], 1)
        self.assertIsNone(by_lot(result, "L1")["award_offer_id"])
        self.assertEqual(by_lot(result, "L2")["award_offer_id"], "O2")
        self.assertEqual(result["package"]["remaining_cents"], 0)

    def test_budget_exact_and_over_budget(self):
        exact = review(
            payload(
                [lot("L1")],
                [offer("O1", "L1", "V1", 100)],
                [vendor("V1", 1)],
                100,
            )
        )
        self.assertEqual(exact["awarded"], 1)
        self.assertEqual(exact["package"]["awarded_total_cents"], 100)
        self.assertEqual(exact["package"]["remaining_cents"], 0)

        over = review(
            payload(
                [lot("L1")],
                [offer("O1", "L1", "V1", 101)],
                [vendor("V1", 1)],
                100,
            )
        )
        self.assertEqual(over["awarded"], 0)
        self.assertEqual(over["package"]["awarded_total_cents"], 0)
        self.assertEqual(over["package"]["remaining_cents"], 100)
        row = offer_row(over, "L1", "O1")
        self.assertTrue(row["eligible"])
        self.assertFalse(row["awarded"])
        self.assertIn("package_budget", row["selection_reasons"])

    def test_budget_choice_avoids_overrun(self):
        result = review(
            payload(
                [lot("L1"), lot("L2")],
                [
                    offer("O1", "L1", "V1", 60),
                    offer("O2", "L1", "V2", 90),
                    offer("O3", "L2", "V3", 50),
                ],
                [vendor("V1", 1), vendor("V2", 1), vendor("V3", 1)],
                100,
            )
        )
        # O1 + O3 = 110 is impossible; O2 + O3 = 140 is impossible; the only
        # two-lot pair would exceed budget, so one lot is awarded.  Equal
        # counts prefer the lower total, which is O3 alone.
        self.assertEqual(result["awarded"], 1)
        self.assertEqual(by_lot(result, "L2")["award_offer_id"], "O3")
        self.assertEqual(result["package"]["awarded_total_cents"], 50)

    def test_overlapping_lots_respect_vendor_capacity(self):
        result = review(
            payload(
                [lot("L1"), lot("L2")],
                [
                    offer("O1", "L1", "V1", 10),
                    offer("O2", "L2", "V1", 10),
                    offer("O3", "L1", "V2", 40),
                    offer("O4", "L2", "V2", 60),
                ],
                [vendor("V1", 1), vendor("V2", 1)],
                1000,
            )
        )
        self.assertEqual(result["awarded"], 2)
        awarded_vendors = {
            by_lot(result, "L1")["award_vendor_id"],
            by_lot(result, "L2")["award_vendor_id"],
        }
        self.assertNotEqual(awarded_vendors, {"V1"})
        # Cheapest feasible pair: both V1 offers cannot run together, so the
        # combination must pair one V1 offer with one V2 offer.  O3+O2 = 50
        # undercuts O1+O4 = 70.
        self.assertEqual(by_lot(result, "L1")["award_offer_id"], "O3")
        self.assertEqual(by_lot(result, "L2")["award_offer_id"], "O2")
        self.assertEqual(result["package"]["awarded_total_cents"], 50)
        v1 = next(x for x in result["package"]["vendors"] if x["vendor_id"] == "V1")
        self.assertEqual(v1["peak_concurrent_lots"], 1)
        self.assertEqual(v1["concurrent_lot_capacity"], 1)

    def test_abutting_service_dates_do_not_overlap(self):
        result = review(
            payload(
                [
                    lot("L1", start="2026-04-01", end="2026-05-01"),
                    lot("L2", start="2026-05-01", end="2026-06-01"),
                ],
                [
                    offer("O1", "L1", "V1", 10),
                    offer("O2", "L2", "V1", 10),
                ],
                [vendor("V1", 1)],
                100,
            )
        )
        self.assertEqual(result["awarded"], 2)
        self.assertEqual(result["lots"][0]["award_vendor_id"], "V1")
        self.assertEqual(result["lots"][1]["award_vendor_id"], "V1")
        v1 = next(x for x in result["package"]["vendors"] if x["vendor_id"] == "V1")
        self.assertEqual(v1["peak_concurrent_lots"], 1)
        self.assertEqual(v1["awarded_lot_ids"], ["L1", "L2"])

    def test_capacity_boundary_allows_exact_peak(self):
        start = "2026-04-01"
        end = "2026-05-01"
        result = review(
            payload(
                [
                    lot("L1", start=start, end=end),
                    lot("L2", start=start, end=end),
                    lot("L3", start=start, end=end),
                ],
                [
                    offer("O1", "L1", "V1", 10),
                    offer("O2", "L2", "V1", 10),
                    offer("O3", "L3", "V1", 10),
                ],
                [vendor("V1", 2)],
                100,
            )
        )
        self.assertEqual(result["awarded"], 2)
        v1 = next(x for x in result["package"]["vendors"] if x["vendor_id"] == "V1")
        self.assertEqual(v1["peak_concurrent_lots"], 2)
        self.assertEqual(v1["concurrent_lot_capacity"], 2)
        # Sequence tie-break leaves the earliest lot unawarded.
        self.assertIsNone(by_lot(result, "L1")["award_offer_id"])
        self.assertEqual(by_lot(result, "L2")["award_offer_id"], "O2")
        self.assertEqual(by_lot(result, "L3")["award_offer_id"], "O3")

    def test_ineligible_offer_does_not_consume_budget_or_capacity(self):
        result = review(
            payload(
                [lot("L1"), lot("L2")],
                [
                    offer("O1", "L1", "V1", 1, documents=[]),
                    offer("O2", "L1", "V2", 40),
                    offer("O3", "L2", "V1", 40),
                ],
                [vendor("V1", 1), vendor("V2", 1)],
                80,
            )
        )
        self.assertEqual(result["awarded"], 2)
        self.assertEqual(by_lot(result, "L1")["award_offer_id"], "O2")
        self.assertEqual(by_lot(result, "L2")["award_offer_id"], "O3")
        self.assertEqual(result["package"]["awarded_total_cents"], 80)
        cheap = offer_row(result, "L1", "O1")
        self.assertFalse(cheap["eligible"])
        self.assertFalse(cheap["awarded"])
        self.assertEqual(cheap["reasons"], ["missing_documents:doc"])
        # The ineligible offer did not take V1 capacity away from L2.
        self.assertEqual(by_lot(result, "L2")["award_vendor_id"], "V1")

    def test_eligible_unawarded_is_distinct_from_ineligible(self):
        result = review(
            payload(
                [lot("L1"), lot("L2")],
                [
                    offer("O1", "L1", "V1", 10),
                    offer("O2", "L2", "V1", 10),
                    offer("O3", "L2", "V2", 500, submitted="2026-03-11T00:00:00+00:00"),
                ],
                [vendor("V1", 1), vendor("V2", 1)],
                1000,
            )
        )
        blocked = offer_row(result, "L1", "O1")
        self.assertTrue(blocked["eligible"])
        self.assertEqual(blocked["reasons"], [])
        self.assertFalse(blocked["awarded"])
        self.assertEqual(blocked["selection_reasons"], ["vendor_capacity"])

        ineligible = offer_row(result, "L2", "O3")
        self.assertFalse(ineligible["eligible"])
        self.assertEqual(ineligible["reasons"], ["late"])
        self.assertFalse(ineligible["awarded"])
        self.assertEqual(ineligible["selection_reasons"], [])

    def test_lot_without_feasible_offer_stays_unawarded(self):
        result = review(
            payload(
                [lot("L1"), lot("L2")],
                [
                    offer("O1", "L1", "V1", 10),
                    offer("O2", "L2", "V1", 10, submitted="2026-03-11T00:00:00+00:00"),
                ],
                [vendor("V1", 1)],
                100,
            )
        )
        self.assertEqual(result["awarded"], 1)
        self.assertEqual(by_lot(result, "L1")["award_offer_id"], "O1")
        self.assertIsNone(by_lot(result, "L2")["award_offer_id"])
        self.assertEqual(by_lot(result, "L2")["offers"][0]["reasons"], ["late"])

    def test_fixture_reflects_package_contract(self):
        result = review(json.loads(Path("fixtures/input.json").read_text()))
        self.assertEqual(result["awarded"], 1)
        self.assertEqual(by_lot(result, "L1")["award_offer_id"], "O1")
        self.assertEqual(by_lot(result, "L1")["award_vendor_id"], "V2")
        self.assertIsNone(by_lot(result, "L2")["award_offer_id"])
        self.assertEqual(by_lot(result, "L2")["offers"][0]["reasons"], ["late"])
        self.assertEqual(result["package"]["budget_cents"], 60000)
        self.assertEqual(result["package"]["awarded_total_cents"], 42000)
        self.assertEqual(result["package"]["remaining_cents"], 18000)
        self.assertEqual(
            next(x for x in result["package"]["vendors"] if x["vendor_id"] == "V1")[
                "peak_concurrent_lots"
            ],
            0,
        )
        self.assertEqual(
            next(x for x in result["package"]["vendors"] if x["vendor_id"] == "V2")[
                "peak_concurrent_lots"
            ],
            1,
        )


class ValidationTests(unittest.TestCase):
    def test_rejects_unknown_lot(self):
        with self.assertRaisesRegex(ValueError, "unknown lot"):
            review(
                payload(
                    [],
                    [offer("o", "x", "v", 1)],
                    [vendor("v", 1)],
                    10,
                )
            )

    def test_rejects_duplicate_ids(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            review(
                payload(
                    [lot("L1"), lot("L1")],
                    [offer("O1", "L1", "V1", 1)],
                    [vendor("V1", 1)],
                    10,
                )
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            review(
                payload(
                    [lot("L1")],
                    [offer("O1", "L1", "V1", 1), offer("O1", "L1", "V1", 2)],
                    [vendor("V1", 1)],
                    10,
                )
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            review(
                payload(
                    [lot("L1")],
                    [offer("O1", "L1", "V1", 1)],
                    [vendor("V1", 1), vendor("V1", 2)],
                    10,
                )
            )

    def test_rejects_unknown_vendor(self):
        with self.assertRaisesRegex(ValueError, "unknown vendor"):
            review(
                payload(
                    [lot("L1")],
                    [offer("O1", "L1", "V9", 1)],
                    [vendor("V1", 1)],
                    10,
                )
            )

    def test_rejects_negative_price(self):
        with self.assertRaisesRegex(ValueError, "price cannot be negative"):
            review(
                payload(
                    [lot("L1")],
                    [offer("O1", "L1", "V1", -1)],
                    [vendor("V1", 1)],
                    10,
                )
            )

    def test_rejects_invalid_service_dates(self):
        for start, end, message in [
            ("2026-04-01", "2026-04-01", "before"),
            ("2026-05-01", "2026-04-01", "before"),
            ("2026/04/01", "2026-05-01", "YYYY-MM-DD"),
            ("2026-02-30", "2026-03-01", "valid YYYY-MM-DD"),
            ("2026-04-01T00:00:00", "2026-05-01", "YYYY-MM-DD"),
        ]:
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(ValueError, message):
                    review(
                        payload(
                            [lot("L1", start=start, end=end)],
                            [offer("O1", "L1", "V1", 1)],
                            [vendor("V1", 1)],
                            10,
                        )
                    )

    def test_rejects_invalid_package_budget(self):
        for budget in (-1, 1.5, "100", True):
            with self.subTest(budget=budget):
                with self.assertRaisesRegex(ValueError, "non-negative integer"):
                    review(
                        payload(
                            [lot("L1")],
                            [offer("O1", "L1", "V1", 1)],
                            [vendor("V1", 1)],
                            budget,
                        )
                    )

    def test_rejects_invalid_vendor_capacity(self):
        for capacity in (0, -1, 1.5, "1", True):
            with self.subTest(capacity=capacity):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    review(
                        payload(
                            [lot("L1")],
                            [offer("O1", "L1", "V1", 1)],
                            [vendor("V1", capacity)],
                            10,
                        )
                    )

    def test_rejects_missing_package_fields(self):
        with self.assertRaisesRegex(ValueError, "concurrent_lot_capacity"):
            review(
                payload(
                    [lot("L1")],
                    [offer("O1", "L1", "V1", 1)],
                    [{"id": "V1"}],
                    10,
                )
            )
        with self.assertRaisesRegex(ValueError, "service_start"):
            result = lot("L1")
            del result["service_start"]
            review(
                payload(
                    [result],
                    [offer("O1", "L1", "V1", 1)],
                    [vendor("V1", 1)],
                    10,
                )
            )
        data = payload(
            [lot("L1")], [offer("O1", "L1", "V1", 1)], [vendor("V1", 1)], 10
        )
        del data["package_budget_cents"]
        with self.assertRaisesRegex(ValueError, "package_budget_cents"):
            review(data)


class CliTests(unittest.TestCase):
    def run_cli(self, input_path, output_path):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "procurement.cli",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent,
        )

    def test_writes_result_and_does_not_overwrite(self):
        data = payload(
            [lot("L1")],
            [offer("O1", "L1", "V1", 10)],
            [vendor("V1", 1)],
            100,
        )
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.json"
            output_path = Path(tmp) / "out" / "decisions.json"
            input_path.write_text(json.dumps(data))
            first = self.run_cli(input_path, output_path)
            self.assertEqual(first.returncode, 0, first.stderr)
            summary = json.loads(first.stdout)
            self.assertEqual(summary["awarded"], 1)
            self.assertEqual(summary["awarded_total_cents"], 10)

            written = json.loads(output_path.read_text())
            self.assertEqual(written["package"]["remaining_cents"], 90)

            second = self.run_cli(input_path, output_path)
            self.assertEqual(second.returncode, 2)
            self.assertIn("File exists", second.stderr)

    def test_invalid_input_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.json"
            output_path = Path(tmp) / "out.json"
            input_path.write_text(
                json.dumps(
                    payload(
                        [lot("L1")],
                        [offer("O1", "L1", "V1", 10)],
                        [vendor("V1", 0)],
                        100,
                    )
                )
            )
            result = self.run_cli(input_path, output_path)
            self.assertEqual(result.returncode, 2)
            self.assertIn("positive integer", result.stderr)
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
