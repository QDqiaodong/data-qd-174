"""Tests for the post-award settlement ledger.

Assertions use observable money and history from reports and state, not a
particular table layout or internal algorithm.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from procurement.settlement import (
    BatchError,
    ExportError,
    StateError,
    apply_batch,
    daily_amounts,
    initialize_ledger,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures


def lot(lot_id, start, end, budget=100000):
    return {
        "id": lot_id,
        "deadline": "2026-03-01T00:00:00+00:00",
        "budget_cents": budget,
        "required_documents": [],
        "service_start": start,
        "service_end": end,
    }


def offer(offer_id, lot_id, vendor_id, price):
    return {
        "id": offer_id,
        "lot_id": lot_id,
        "vendor_id": vendor_id,
        "submitted": "2026-02-01T00:00:00+00:00",
        "price_cents": price,
        "documents": [],
    }


def base_package():
    """L1: three days and 100 cents; L2: one day and 50; L3: one day and 40."""
    return {
        "package_budget_cents": 1000,
        "vendors": [
            {"id": "V1", "concurrent_lot_capacity": 1},
            {"id": "V2", "concurrent_lot_capacity": 1},
        ],
        "lots": [
            lot("L1", "2026-04-01", "2026-04-04"),
            lot("L2", "2026-05-01", "2026-05-02"),
            lot("L3", "2026-06-01", "2026-06-02"),
        ],
        "offers": [
            offer("O1", "L1", "V1", 100),
            offer("O2", "L2", "V1", 50),
            offer("O3", "L3", "V2", 40),
        ],
    }


def receipt(event_id, receipt_id, revision, lot_id, ranges):
    return {
        "event_id": event_id,
        "kind": "receipt",
        "receipt_id": receipt_id,
        "revision": revision,
        "lot_id": lot_id,
        "accepted_ranges": [
            {"from_date": start, "to_date": end} for start, end in ranges
        ],
    }


def invoice(event_id, invoice_id, lot_id, start, end):
    return {
        "event_id": event_id,
        "kind": "invoice",
        "invoice_id": invoice_id,
        "lot_id": lot_id,
        "from_date": start,
        "to_date": end,
    }


def settle(event_id, budget):
    return {
        "event_id": event_id,
        "kind": "settle",
        "cash_budget_cents": budget,
    }


def batch(batch_id, events):
    return {"batch_id": batch_id, "events": events}


def scenario_b1():
    return batch(
        "B1",
        [
            receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-02")]),
            invoice("e2", "I1", "L1", "2026-04-01", "2026-04-02"),
            settle("e3", 1000),
        ],
    )


def scenario_b2():
    return batch(
        "B2",
        [
            # Correct R1: day 2026-04-02 is no longer accepted.
            receipt("e4", "R1", 2, "L1", [("2026-04-01", "2026-04-01")]),
            receipt("e5", "R2", 1, "L2", [("2026-05-01", "2026-05-01")]),
            invoice("e7", "I2", "L2", "2026-05-01", "2026-05-01"),
            invoice("e8", "I3", "L3", "2026-06-01", "2026-06-01"),
            settle("e9", 20),
        ],
    )


def by_id(rows, key, value):
    return next(row for row in rows if row[key] == value)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.state = self.dir / "state.json"

    def init(self, package=None):
        input_path = self.dir / "package.json"
        input_path.write_text(json.dumps(package or base_package()))
        initialize_ledger(input_path, self.state)
        return input_path

    def apply(self, value, output=None):
        return apply_batch(self.state, value, output)


# ---------------------------------------------------------------------------
# Daily allocation


class DailyAmountTests(unittest.TestCase):
    def test_remainder_goes_to_earliest_days(self):
        amounts = daily_amounts("2026-04-01", "2026-04-04", 100)
        self.assertEqual(
            [(day.isoformat(), cents) for day, cents in amounts.items()],
            [("2026-04-01", 34), ("2026-04-02", 33), ("2026-04-03", 33)],
        )
        self.assertEqual(sum(amounts.values()), 100)

    def test_crosses_month_year_and_keeps_total(self):
        amounts = daily_amounts("2026-12-30", "2027-01-03", 10)
        self.assertEqual([day.isoformat() for day in amounts], [
            "2026-12-30",
            "2026-12-31",
            "2027-01-01",
            "2027-01-02",
        ])
        self.assertEqual(list(amounts.values()), [3, 3, 2, 2])
        self.assertEqual(sum(amounts.values()), 10)

    def test_leap_day(self):
        amounts = daily_amounts("2028-02-28", "2028-03-02", 100)
        self.assertIn(date(2028, 2, 29), amounts)
        self.assertEqual(list(amounts.values()), [34, 33, 33])
        self.assertEqual(sum(amounts.values()), 100)

    def test_zero_price_contract(self):
        amounts = daily_amounts("2026-04-01", "2026-04-06", 0)
        self.assertEqual(len(amounts), 5)
        self.assertEqual(set(amounts.values()), {0})
        self.assertEqual(sum(amounts.values()), 0)

    def test_empty_interval_rejected(self):
        with self.assertRaises(ValueError):
            daily_amounts("2026-04-01", "2026-04-01", 10)


# ---------------------------------------------------------------------------
# Initialization


class InitTests(LedgerCase):
    def test_freezes_award_and_contract_totals(self):
        input_path = self.init()
        state = json.loads(self.state.read_text())
        self.assertEqual(state["award"]["awarded"], 3)
        self.assertEqual(state["award"]["awarded_total_cents"], 190)
        self.assertEqual(
            {lot_id: (row["vendor_id"], row["price_cents"]) for lot_id, row in state["contracts"].items()},
            {"L1": ("V1", 100), "L2": ("V1", 50), "L3": ("V2", 40)},
        )
        self.assertEqual(
            state["contracts"]["L1"]["daily"],
            {"2026-04-01": 34, "2026-04-02": 33, "2026-04-03": 33},
        )
        # Later changes to the input file cannot re-run the award.
        input_path.write_text("{}")
        report = self.apply(scenario_b1())
        self.assertEqual(report["cash_paid_cents"], 67)

    def test_refuses_existing_state(self):
        self.init()
        before = self.state.read_bytes()
        with self.assertRaisesRegex(StateError, "already exists"):
            self.init()
        self.assertEqual(self.state.read_bytes(), before)

    def test_invalid_input_leaves_no_state(self):
        good = base_package()
        good["vendors"][0]["concurrent_lot_capacity"] = 0
        input_path = self.dir / "bad.json"
        input_path.write_text(json.dumps(good))
        with self.assertRaises(ValueError):
            initialize_ledger(input_path, self.state)
        self.assertFalse(self.state.exists())

    def test_only_awarded_lots_enter_business(self):
        package = base_package()
        package["package_budget_cents"] = 0
        self.init(package)
        with self.assertRaises(BatchError) as ctx:
            self.apply(batch("B1", [receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-01")])]))
        self.assertIn("unknown or unawarded lot", str(ctx.exception))
        self.assertFalse((self.dir / "report.json").exists())


# ---------------------------------------------------------------------------
# Core linked business scenario


class LinkedScenarioTests(LedgerCase):
    def run_all(self):
        self.init()
        b1 = self.apply(scenario_b1())
        b2 = self.apply(scenario_b2())
        b3 = self.apply(
            batch(
                "B3",
                [
                    receipt("e10", "R1", 3, "L1", [("2026-04-01", "2026-04-02")]),
                    settle("e11", 0),
                ],
            )
        )
        return b1, b2, b3

    def test_first_payment_uses_accepted_days(self):
        b1, _, _ = self.run_all()
        settlement = b1["settlements"][0]
        self.assertEqual(settlement["cash_paid_cents"], 67)
        self.assertEqual(settlement["unused_budget_cents"], 933)
        self.assertEqual(
            settlement["cash_payments"],
            [{"invoice_id": "I1", "vendor_id": "V1", "amount_cents": 67}],
        )
        invoice_row = by_id(b1["invoices"], "invoice_id", "I1")
        self.assertEqual(invoice_row["claimed_cents"], 67)
        self.assertEqual(invoice_row["earned_cents"], 67)
        self.assertEqual(invoice_row["due_cents"], 0)

    def test_correction_creates_recoverable_without_erasing_cash(self):
        b1, b2, _ = self.run_all()
        settlement = b2["settlements"][0]
        self.assertEqual(
            settlement["offsets"],
            [
                {
                    "from_invoice_id": "I1",
                    "to_invoice_id": "I2",
                    "vendor_id": "V1",
                    "amount_cents": 33,
                }
            ],
        )
        self.assertEqual(
            settlement["cash_payments"],
            [{"invoice_id": "I2", "vendor_id": "V1", "amount_cents": 17}],
        )
        self.assertEqual(settlement["cash_paid_cents"], 17)
        self.assertEqual(settlement["unused_budget_cents"], 3)

        i1 = by_id(b2["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["cash_paid_cents"], 67)  # history unchanged
        self.assertEqual(i1["recovered_cents"], 33)
        self.assertEqual(i1["earned_cents"], 34)
        self.assertEqual(i1["pending_acceptance_cents"], 33)
        self.assertEqual(i1["due_cents"], 0)
        self.assertEqual(i1["recoverable_cents"], 0)

        i2 = by_id(b2["invoices"], "invoice_id", "I2")
        self.assertEqual(i2["claimed_cents"], 50)
        self.assertEqual(i2["offset_in_cents"], 33)
        self.assertEqual(i2["cash_paid_cents"], 17)
        self.assertEqual(i2["due_cents"], 0)
        self.assertEqual(i2["net_settled_cents"], 50)

    def test_recovery_never_crosses_vendors_even_with_due_targets(self):
        self.init()
        # I1 is overpaid after correction; I3 (V2) is fully accepted and due.
        # A zero-budget settle must not move V1's recovery to V2.
        report = self.apply(
            batch(
                "B1",
                [
                    receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-02")]),
                    receipt("e2", "R2", 1, "L3", [("2026-06-01", "2026-06-01")]),
                    invoice("e3", "I1", "L1", "2026-04-01", "2026-04-02"),
                    invoice("e4", "I3", "L3", "2026-06-01", "2026-06-01"),
                    settle("e5", 67),
                    receipt("e6", "R1", 2, "L1", [("2026-04-01", "2026-04-01")]),
                    settle("e7", 0),
                ],
            )
        )
        second = report["settlements"][1]
        self.assertEqual(second["offsets"], [])
        self.assertEqual(second["cash_paid_cents"], 0)
        i1 = by_id(report["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["recoverable_cents"], 33)
        i3 = by_id(report["invoices"], "invoice_id", "I3")
        self.assertEqual(i3["due_cents"], 40)
        self.assertEqual(i3["offset_in_cents"], 0)

    def test_later_events_in_same_batch_do_not_rewrite_earlier_settlement(self):
        self.init()
        report = self.apply(
            batch(
                "B1",
                [
                    receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-02")]),
                    invoice("e2", "I1", "L1", "2026-04-01", "2026-04-02"),
                    settle("e3", 100),
                    # Correction after the settlement: the payment detail above
                    # must keep reflecting the facts at its own point in time.
                    receipt("e4", "R1", 2, "L1", [("2026-04-01", "2026-04-01")]),
                ],
            )
        )
        settlement = report["settlements"][0]
        self.assertEqual(settlement["cash_paid_cents"], 67)
        self.assertEqual(
            settlement["cash_payments"],
            [{"invoice_id": "I1", "vendor_id": "V1", "amount_cents": 67}],
        )
        i1 = by_id(report["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["cash_paid_cents"], 67)
        self.assertEqual(i1["earned_cents"], 34)
        self.assertEqual(i1["recoverable_cents"], 33)

    def test_other_vendor_cannot_absorb_the_recovery(self):
        _, b2, _ = self.run_all()
        i3 = by_id(b2["invoices"], "invoice_id", "I3")
        self.assertEqual(i3["vendor_id"], "V2")
        self.assertEqual(i3["due_cents"], 0)
        self.assertEqual(i3["pending_acceptance_cents"], 40)
        self.assertEqual(i3["cash_paid_cents"], 0)
        self.assertEqual(i3["offset_in_cents"], 0)
        for row in b2["settlements"][0]["offsets"]:
            self.assertEqual(row["vendor_id"], "V1")

    def test_restored_acceptance_reopens_due(self):
        _, _, b3 = self.run_all()
        i1 = by_id(b3["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["earned_cents"], 67)
        self.assertEqual(i1["net_settled_cents"], 34)
        self.assertEqual(i1["due_cents"], 33)
        self.assertEqual(i1["recoverable_cents"], 0)
        self.assertEqual(i1["cash_paid_cents"], 67)
        self.assertEqual(b3["settlements"][0]["cash_paid_cents"], 0)
        self.assertEqual(b3["settlements"][0]["unused_budget_cents"], 0)

    def test_due_is_paid_after_restoration(self):
        self.run_all()
        b4 = self.apply(batch("B4", [settle("e12", 33)]))
        self.assertEqual(b4["settlements"][0]["cash_payments"],
                         [{"invoice_id": "I1", "vendor_id": "V1", "amount_cents": 33}])
        self.assertEqual(b4["settlements"][0]["unused_budget_cents"], 0)
        # I3 (V2) is still entirely pending acceptance.
        i3 = by_id(b4["invoices"], "invoice_id", "I3")
        self.assertEqual(i3["due_cents"], 0)
        self.assertEqual(i3["pending_acceptance_cents"], 40)

    def test_settle_before_any_due_succeeds(self):
        self.init()
        report = self.apply(batch("B1", [settle("e1", 500)]))
        self.assertEqual(report["settlements"][0]["cash_paid_cents"], 0)
        self.assertEqual(report["settlements"][0]["unused_budget_cents"], 500)
        self.assertEqual(report["invoices"], [])
        self.assertEqual(report["cash_paid_cents"], 0)


# ---------------------------------------------------------------------------
# Receipts


class ReceiptTests(LedgerCase):
    def test_overlapping_receipts_and_single_revocation(self):
        self.init()
        b1 = self.apply(
            batch(
                "B1",
                [
                    receipt("e1", "RA", 1, "L1", [("2026-04-01", "2026-04-03")]),
                    receipt("e2", "RB", 1, "L1", [("2026-04-02", "2026-04-03")]),
                    invoice("e3", "I1", "L1", "2026-04-01", "2026-04-03"),
                    settle("e4", 100),
                ],
            )
        )
        self.assertEqual(b1["cash_paid_cents"], 100)
        # Revoking RB removes no day because RA still covers them all.
        b2 = self.apply(
            batch(
                "B2",
                [
                    receipt("e5", "RB", 2, "L1", []),
                    settle("e6", 100),
                ],
            )
        )
        i1 = by_id(b2["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["earned_cents"], 100)
        self.assertEqual(i1["recoverable_cents"], 0)
        self.assertEqual(b2["settlements"][0]["offsets"], [])
        self.assertEqual(b2["settlements"][0]["cash_paid_cents"], 0)

    def test_adjacent_ranges_are_allowed_and_empty_revokes(self):
        self.init()
        report = self.apply(
            batch(
                "B1",
                [
                    receipt(
                        "e1",
                        "R1",
                        1,
                        "L1",
                        [("2026-04-01", "2026-04-01"), ("2026-04-02", "2026-04-02")],
                    ),
                    invoice("e2", "I1", "L1", "2026-04-01", "2026-04-02"),
                    settle("e3", 100),
                ],
            )
        )
        self.assertEqual(report["cash_paid_cents"], 67)
        revoked = self.apply(
            batch("B2", [receipt("e4", "R1", 2, "L1", []), settle("e5", 0)])
        )
        i1 = by_id(revoked["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["earned_cents"], 0)
        self.assertEqual(i1["recoverable_cents"], 67)
        self.assertEqual(i1["cash_paid_cents"], 67)

    def test_overlapping_ranges_in_one_receipt_rejected(self):
        self.init()
        with self.assertRaisesRegex(BatchError, "overlapping ranges"):
            self.apply(
                batch(
                    "B1",
                    [
                        receipt(
                            "e1",
                            "R1",
                            1,
                            "L1",
                            [("2026-04-01", "2026-04-02"), ("2026-04-02", "2026-04-03")],
                        )
                    ],
                )
            )

    def test_revision_rules(self):
        self.init()
        with self.assertRaisesRegex(BatchError, "revision must be 1"):
            self.apply(batch("B1", [receipt("e1", "R1", 2, "L1", [])]))

        self.apply(batch("B2", [receipt("e2", "R1", 1, "L1", [("2026-04-01", "2026-04-01")])]))
        with self.assertRaisesRegex(BatchError, "expected revision 2"):
            self.apply(batch("B3", [receipt("e3", "R1", 3, "L1", [])]))
        with self.assertRaisesRegex(BatchError, "expected revision 2"):
            self.apply(batch("B3", [receipt("e3", "R1", 1, "L1", [])]))
        self.apply(batch("B4", [receipt("e4", "R1", 2, "L1", [])]))
        with self.assertRaisesRegex(BatchError, "expected revision 3"):
            self.apply(batch("B5", [receipt("e5", "R1", 2, "L1", [])]))

    def test_lot_identity_is_immutable(self):
        self.init()
        self.apply(batch("B1", [receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-01")])]))
        with self.assertRaisesRegex(BatchError, "lot_id cannot change"):
            self.apply(batch("B2", [receipt("e2", "R1", 2, "L2", [("2026-05-01", "2026-05-01")])]))

    def test_dates_must_stay_inside_contract(self):
        self.init()
        for ranges in (
            [("2026-03-31", "2026-04-01")],
            [("2026-04-03", "2026-04-04")],  # service_end is exclusive
            [("2026-04-03", "2026-03-01")],
        ):
            with self.subTest(ranges=ranges):
                with self.assertRaises(BatchError):
                    self.apply(batch("B", [receipt("e", "R", 1, "L1", ranges)]))


# ---------------------------------------------------------------------------
# Invoices


class InvoiceTests(LedgerCase):
    def test_invoice_before_acceptance_keeps_pending_amount(self):
        self.init()
        b1 = self.apply(
            batch(
                "B1",
                [invoice("e1", "I1", "L1", "2026-04-01", "2026-04-02"), settle("e2", 500)],
            )
        )
        i1 = by_id(b1["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["claimed_cents"], 67)
        self.assertEqual(i1["earned_cents"], 0)
        self.assertEqual(i1["pending_acceptance_cents"], 67)
        self.assertEqual(i1["due_cents"], 0)
        self.assertEqual(b1["settlements"][0]["cash_paid_cents"], 0)
        self.assertEqual(b1["settlements"][0]["unused_budget_cents"], 500)

        b2 = self.apply(
            batch(
                "B2",
                [
                    receipt("e3", "R1", 1, "L1", [("2026-04-01", "2026-04-02")]),
                    settle("e4", 500),
                ],
            )
        )
        i1 = by_id(b2["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["pending_acceptance_cents"], 0)
        self.assertEqual(i1["cash_paid_cents"], 67)

    def test_claimed_amount_comes_from_contract_not_caller(self):
        self.init()
        event = invoice("e1", "I1", "L1", "2026-04-01", "2026-04-03")
        event["amount_cents"] = 1
        with self.assertRaisesRegex(BatchError, "unknown field"):
            self.apply(batch("B1", [event]))

    def test_invoice_overlaps_rejected_but_adjacent_allowed(self):
        self.init()
        self.apply(batch("B1", [invoice("e1", "I1", "L1", "2026-04-01", "2026-04-01")]))
        with self.assertRaisesRegex(BatchError, "overlaps invoice"):
            self.apply(batch("B2", [invoice("e2", "I2", "L1", "2026-04-01", "2026-04-02")]))
        report = self.apply(batch("B3", [invoice("e3", "I3", "L1", "2026-04-02", "2026-04-03")]))
        self.assertEqual(by_id(report["invoices"], "invoice_id", "I3")["claimed_cents"], 66)

    def test_invoice_id_is_immutable(self):
        self.init()
        self.apply(batch("B1", [invoice("e1", "I1", "L1", "2026-04-01", "2026-04-01")]))
        with self.assertRaisesRegex(BatchError, "immutable|already exists"):
            self.apply(batch("B2", [invoice("e2", "I1", "L2", "2026-05-01", "2026-05-01")]))

    def test_invoice_outside_service_interval_rejected(self):
        self.init()
        with self.assertRaisesRegex(BatchError, "outside"):
            self.apply(batch("B1", [invoice("e1", "I1", "L1", "2026-04-02", "2026-04-04")]))

    def test_zero_price_contract_invoice(self):
        package = {
            "package_budget_cents": 10,
            "vendors": [{"id": "V1", "concurrent_lot_capacity": 1}],
            "lots": [lot("L1", "2026-04-01", "2026-04-04")],
            "offers": [offer("O1", "L1", "V1", 0)],
        }
        self.init(package)
        report = self.apply(
            batch(
                "B1",
                [
                    receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-03")]),
                    invoice("e2", "I1", "L1", "2026-04-01", "2026-04-03"),
                    settle("e3", 100),
                ],
            )
        )
        i1 = by_id(report["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["claimed_cents"], 0)
        self.assertEqual(i1["due_cents"], 0)
        self.assertEqual(report["cash_paid_cents"], 0)
        self.assertEqual(report["unused_budget_cents"], 100)
        self.assertEqual(report["lots"][0]["contract_cents"], 0)
        self.assertEqual(report["lots"][0]["daily_amounts"],
                         {"2026-04-01": 0, "2026-04-02": 0, "2026-04-03": 0})


# ---------------------------------------------------------------------------
# Settlement ordering


class SettlementTests(LedgerCase):
    def test_payment_order_is_global_by_invoice_seq_across_vendors(self):
        # I2 (V1) is issued first, I3 (V2) second.  A 20-cent budget must
        # fully clear I2 in seq order rather than split between vendors.
        self.init()
        report = self.apply(
            batch(
                "B1",
                [
                    receipt("e1", "R1", 1, "L2", [("2026-05-01", "2026-05-01")]),
                    receipt("e2", "R2", 1, "L3", [("2026-06-01", "2026-06-01")]),
                    invoice("e3", "I2", "L2", "2026-05-01", "2026-05-01"),
                    invoice("e4", "I3", "L3", "2026-06-01", "2026-06-01"),
                    settle("e5", 20),
                ],
            )
        )
        self.assertEqual(
            report["settlements"][0]["cash_payments"],
            [{"invoice_id": "I2", "vendor_id": "V1", "amount_cents": 20}],
        )
        self.assertEqual(by_id(report["invoices"], "invoice_id", "I2")["due_cents"], 30)
        self.assertEqual(by_id(report["invoices"], "invoice_id", "I3")["due_cents"], 40)

    def test_zero_budget_still_offsets(self):
        self.init()
        self.apply(
            batch(
                "B1",
                [
                    receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-02")]),
                    receipt("e2", "R2", 1, "L2", [("2026-05-01", "2026-05-01")]),
                    invoice("e3", "I1", "L1", "2026-04-01", "2026-04-02"),
                    settle("e4", 100),
                    receipt("e5", "R1", 2, "L1", [("2026-04-01", "2026-04-01")]),
                    invoice("e6", "I2", "L2", "2026-05-01", "2026-05-01"),
                ],
            )
        )
        report = self.apply(batch("B2", [settle("e7", 0)]))
        settlement = report["settlements"][0]
        self.assertEqual(settlement["cash_budget_cents"], 0)
        self.assertEqual(settlement["cash_paid_cents"], 0)
        self.assertEqual(settlement["unused_budget_cents"], 0)
        self.assertEqual(settlement["offsets"][0]["amount_cents"], 33)
        self.assertEqual(settlement["offsets"][0]["from_invoice_id"], "I1")
        self.assertEqual(settlement["offsets"][0]["to_invoice_id"], "I2")

    def test_payments_follow_invoice_seq_and_can_be_partial(self):
        self.init()
        report = self.apply(
            batch(
                "B1",
                [
                    receipt("e1", "R1", 1, "L2", [("2026-05-01", "2026-05-01")]),
                    receipt("e2", "R2", 1, "L3", [("2026-06-01", "2026-06-01")]),
                    invoice("e3", "I2", "L2", "2026-05-01", "2026-05-01"),
                    invoice("e4", "I3", "L3", "2026-06-01", "2026-06-01"),
                    settle("e5", 60),
                ],
            )
        )
        self.assertEqual(
            report["settlements"][0]["cash_payments"],
            [
                {"invoice_id": "I2", "vendor_id": "V1", "amount_cents": 50},
                {"invoice_id": "I3", "vendor_id": "V2", "amount_cents": 10},
            ],
        )
        self.assertEqual(by_id(report["invoices"], "invoice_id", "I3")["due_cents"], 30)
        self.assertEqual(report["unused_budget_cents"], 0)

    def test_unused_budget_is_not_rolled_over(self):
        self.init()
        self.apply(
            batch(
                "B1",
                [
                    receipt("e1", "R1", 1, "L2", [("2026-05-01", "2026-05-01")]),
                    invoice("e2", "I2", "L2", "2026-05-01", "2026-05-01"),
                    settle("e3", 1000),
                ],
            )
        )
        report = self.apply(batch("B2", [settle("e4", 10)]))
        self.assertEqual(report["settlements"][0]["cash_paid_cents"], 0)
        self.assertEqual(report["settlements"][0]["unused_budget_cents"], 10)


# ---------------------------------------------------------------------------
# Batch atomicity and validation


class BatchTests(LedgerCase):
    def test_failed_batch_leaves_nothing_and_frees_ids(self):
        self.init()
        failing = batch(
            "BX",
            [
                receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-01")]),
                invoice("e2", "I1", "L1", "2026-04-01", "2026-04-09"),
            ],
        )
        output = self.dir / "report.json"
        with self.assertRaises(BatchError):
            self.apply(failing, output)
        self.assertFalse(output.exists())

        # The same batch ID and event IDs are still available.
        report = self.apply(
            batch(
                "BX",
                [
                    receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-01")]),
                    settle("e3", 100),
                ],
            )
        )
        self.assertEqual(report["events"][0]["seq"], 1)
        self.assertEqual(report["events"][1]["seq"], 2)

    def test_duplicate_event_ids_in_batch_rejected(self):
        self.init()
        with self.assertRaisesRegex(BatchError, "duplicated in the batch"):
            self.apply(
                batch(
                    "B1",
                    [
                        receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-01")]),
                        settle("e1", 10),
                    ],
                )
            )

    def test_event_ids_are_global(self):
        self.init()
        self.apply(batch("B1", [settle("e1", 0)]))
        with self.assertRaisesRegex(BatchError, "already used"):
            self.apply(batch("B2", [settle("e1", 0)]))

    def test_unknown_kind_and_fields(self):
        self.init()
        with self.assertRaisesRegex(BatchError, "unknown kind"):
            self.apply(batch("B1", [{"event_id": "e1", "kind": "refund"}]))
        with self.assertRaisesRegex(BatchError, "missing"):
            self.apply(batch("B2", [{"event_id": "e1", "kind": "settle"}]))

    def test_identifiers_and_integers_are_strict(self):
        self.init()
        cases = [
            batch("", []),
            batch("B1", [{"event_id": "", "kind": "settle", "cash_budget_cents": 0}]),
            batch("B1", [settle("e1", True)]),
            batch("B1", [settle("e1", -1)]),
            batch("B1", [settle("e1", 1.0)]),
            batch("B1", [receipt("e1", "R1", True, "L1", [])]),
            batch("B1", [{"batch_id": "B1", "events": []}]),
        ]
        for index, value in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(BatchError):
                    self.apply(value)

    def test_programmatic_non_finite_event_is_rejected(self):
        self.init()
        with self.assertRaises(BatchError):
            self.apply(
                batch(
                    "B1",
                    [
                        {
                            "event_id": "e1",
                            "kind": "settle",
                            "cash_budget_cents": float("nan"),
                        }
                    ],
                )
            )

    def test_device_permutation_does_not_bypass_replay_identity(self):
        self.init()
        original = scenario_b1()
        self.apply(original)
        # Same structure and values, different textual layout and key order,
        # parsed back to a Python object: still an exact replay.
        text = json.dumps(original, indent=4, sort_keys=False)
        reparsed = json.loads(text)
        parsed_path = self.dir / "reparsed.json"
        parsed_path.write_text(json.dumps(reparsed))
        report = apply_batch(self.state, parsed_path)
        self.assertEqual(report, self.apply(original))

    def test_empty_batch_is_valid(self):
        self.init()
        report = self.apply(batch("B1", []))
        self.assertEqual(report["events"], [])
        self.assertEqual(report["settlements"], [])
        self.assertEqual(report["cash_paid_cents"], 0)


# ---------------------------------------------------------------------------
# Replay and conflicts


class ReplayTests(LedgerCase):
    def test_exact_replay_returns_persisted_report_after_later_batches(self):
        self.init()
        b1 = self.apply(scenario_b1())
        self.apply(scenario_b2())
        replay = self.apply(scenario_b1())
        self.assertEqual(replay, b1)
        # No new sequence numbers or payments were created.
        self.assertEqual(replay["events"][-1]["seq"], 3)
        i1 = by_id(replay["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["cash_paid_cents"], 67)

    def test_replay_ignores_object_key_order(self):
        self.init()
        original = scenario_b1()
        self.apply(original)
        reordered = {
            "events": [
                {
                    **{key: event[key] for key in reversed(list(event))},
                }
                for event in original["events"]
            ],
            "batch_id": original["batch_id"],
        }
        report = self.apply(reordered)
        self.assertEqual(report, self.apply(original))

    def test_same_batch_id_with_different_content_conflicts(self):
        self.init()
        self.apply(scenario_b1())
        changed = scenario_b1()
        changed["events"][2]["cash_budget_cents"] = 1
        with self.assertRaisesRegex(BatchError, "different content"):
            self.apply(changed)
        emptied = {"batch_id": "B1", "events": []}
        with self.assertRaisesRegex(BatchError, "different content"):
            self.apply(emptied)

    def test_replay_does_not_reread_mutable_revisions(self):
        self.init()
        self.apply(scenario_b1())
        self.apply(scenario_b2())  # R1 is now at revision 2
        report = self.apply(scenario_b1())  # contains revision 1
        self.assertEqual(report["batch_id"], "B1")
        self.assertEqual(by_id(report["invoices"], "invoice_id", "I1")["cash_paid_cents"], 67)


# ---------------------------------------------------------------------------
# Persistence and reports


class PersistenceTests(LedgerCase):
    def test_sequence_continues_across_state_reloads(self):
        self.init()
        r1 = self.apply(batch("B1", [settle("e1", 0)]))
        self.assertEqual(r1["events"][0]["seq"], 1)
        # apply_batch reloads from disk on every call; a second call must
        # continue the global sequence.
        r2 = self.apply(batch("B2", [settle("e2", 0), settle("e3", 0)]))
        self.assertEqual([row["seq"] for row in r2["events"]], [2, 3])

    def test_report_is_stable_and_reconcilable(self):
        self.init()
        report = self.apply(
            batch(
                "B1",
                [
                    receipt("e1", "R1", 1, "L1", [("2026-04-01", "2026-04-02")]),
                    invoice("e2", "I1", "L1", "2026-04-01", "2026-04-02"),
                    settle("e3", 40),
                ],
            )
        )
        self.assertEqual(report["schema_version"], 1)
        for key in ("batch_id", "events", "settlements", "lots", "invoices"):
            self.assertIn(key, report)
        lot_row = report["lots"][0]
        self.assertEqual(sum(lot_row["daily_amounts"].values()), lot_row["contract_cents"])
        accepted = sum(
            cents
            for day, cents in lot_row["daily_amounts"].items()
            if "2026-04-01" <= day <= "2026-04-02"
        )
        self.assertEqual(lot_row["accepted_cents"], accepted)
        self.assertEqual(sum(row["claimed_cents"] for row in report["invoices"]), accepted)
        self.assertEqual(
            sum(row["cash_paid_cents"] for row in report["invoices"]),
            report["cash_paid_cents"],
        )

    def test_unknown_or_corrupt_state_is_rejected(self):
        self.init()
        original = self.state.read_text()
        self.state.write_text(original[:-10])
        with self.assertRaises(StateError):
            self.apply(batch("B1", []))
        state = json.loads(original)
        state["schema_version"] = 99
        self.state.write_text(json.dumps(state))
        with self.assertRaisesRegex(StateError, "schema_version"):
            self.apply(batch("B1", []))
        self.state.write_text(json.dumps({"schema": "something-else"}))
        with self.assertRaises(StateError):
            self.apply(batch("B1", []))
        self.state.write_text(original)
        self.assertEqual(self.apply(batch("B1", []))["batch_id"], "B1")

    def test_batch_is_committed_before_export_failure(self):
        self.init()
        blocker = self.dir / "blocked"
        blocker.write_text("not a directory")
        output = blocker / "report.json"
        with self.assertRaises(ExportError) as ctx:
            self.apply(scenario_b1(), output)
        self.assertTrue(ctx.exception.batch_committed)
        self.assertEqual(ctx.exception.report["batch_id"], "B1")

        # A later replay writes the same durable report without paying twice.
        report = self.apply(scenario_b1(), self.dir / "report.json")
        self.assertEqual(report["cash_paid_cents"], 67)
        i1 = by_id(report["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["cash_paid_cents"], 67)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["invoices"]["I1"]["cash_paid_cents"], 67)

    def test_report_file_is_not_overwritten_with_different_bytes(self):
        self.init()
        output = self.dir / "report.json"
        output.write_text("{}")
        with self.assertRaisesRegex(ExportError, "different content"):
            self.apply(scenario_b1(), output)
        self.assertEqual(output.read_text(), "{}")
        # The batch is committed and may be exported elsewhere.
        report = self.apply(scenario_b1(), self.dir / "other.json")
        self.assertEqual(report["cash_paid_cents"], 67)


# ---------------------------------------------------------------------------
# CLI


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.state = self.dir / "state.json"
        self.package_path = self.dir / "package.json"
        self.package_path.write_text(json.dumps(base_package()))

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "procurement.settle_cli", *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    def write(self, name, value):
        path = self.dir / name
        path.write_text(json.dumps(value))
        return path

    def test_init_and_apply_stdout_matches_file(self):
        init = self.run_cli(
            "init", "--input", str(self.package_path), "--state", str(self.state)
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        summary = json.loads(init.stdout)
        self.assertEqual(summary["awarded_lots"], ["L1", "L2", "L3"])

        batch_path = self.write("b1.json", scenario_b1())
        output = self.dir / "out" / "b1-report.json"
        applied = self.run_cli(
            "apply",
            "--state",
            str(self.state),
            "--batch",
            str(batch_path),
            "--output",
            str(output),
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout), json.loads(output.read_text()))

        replay = self.run_cli(
            "apply",
            "--state",
            str(self.state),
            "--batch",
            str(batch_path),
            "--output",
            str(output),
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout), json.loads(output.read_text()))

    def test_export_failure_then_new_process_replay(self):
        self.assertEqual(
            self.run_cli(
                "init", "--input", str(self.package_path), "--state", str(self.state)
            ).returncode,
            0,
        )
        batch_path = self.write("b1.json", scenario_b1())
        blocker = self.dir / "blocked"
        blocker.write_text("x")
        failed = self.run_cli(
            "apply",
            "--state",
            str(self.state),
            "--batch",
            str(batch_path),
            "--output",
            str(blocker / "report.json"),
        )
        self.assertEqual(failed.returncode, 2)
        self.assertIn("WAS ACCEPTED", failed.stderr)

        output = self.dir / "report.json"
        retried = self.run_cli(
            "apply",
            "--state",
            str(self.state),
            "--batch",
            str(batch_path),
            "--output",
            str(output),
        )
        self.assertEqual(retried.returncode, 0, retried.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["cash_paid_cents"], 67)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["invoices"]["I1"]["cash_paid_cents"], 67)
        self.assertEqual(state["next_seq"], 4)

    def test_consecutive_batches_across_processes(self):
        self.assertEqual(
            self.run_cli(
                "init", "--input", str(self.package_path), "--state", str(self.state)
            ).returncode,
            0,
        )
        b1_path = self.write("b1.json", scenario_b1())
        b2_path = self.write("b2.json", scenario_b2())
        first = self.run_cli(
            "apply", "--state", str(self.state), "--batch", str(b1_path),
            "--output", str(self.dir / "r1.json"),
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_cli(
            "apply", "--state", str(self.state), "--batch", str(b2_path),
            "--output", str(self.dir / "r2.json"),
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        report = json.loads(second.stdout)
        self.assertEqual(
            [row["seq"] for row in report["events"]], [4, 5, 6, 7, 8]
        )
        i1 = by_id(report["invoices"], "invoice_id", "I1")
        self.assertEqual(i1["cash_paid_cents"], 67)  # from the previous process
        self.assertEqual(i1["recovered_cents"], 33)
        self.assertEqual(report["settlements"][0]["unused_budget_cents"], 3)

        # Old batch replayed by yet another process returns its original report.
        replay = self.run_cli(
            "apply", "--state", str(self.state), "--batch", str(b1_path),
            "--output", str(self.dir / "r1-again.json"),
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(
            json.loads(replay.stdout), json.loads((self.dir / "r1.json").read_text())
        )
        self.assertEqual(replay.stdout, first.stdout)

    def test_business_error_leaves_no_report_and_exits_2(self):
        self.assertEqual(
            self.run_cli(
                "init", "--input", str(self.package_path), "--state", str(self.state)
            ).returncode,
            0,
        )
        bad = self.write("bad.json", batch("B1", [invoice("e1", "I1", "L9", "2026-01-01", "2026-01-02")]))
        output = self.dir / "bad-report.json"
        result = self.run_cli(
            "apply", "--state", str(self.state), "--batch", str(bad), "--output", str(output)
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("B1", result.stderr)
        self.assertFalse(output.exists())
        state = json.loads(self.state.read_text())
        self.assertEqual(state["next_seq"], 1)
        self.assertEqual(state["batches"], {})

    def test_invalid_json_duplicate_keys_and_non_finite_rejected(self):
        self.assertEqual(
            self.run_cli(
                "init", "--input", str(self.package_path), "--state", str(self.state)
            ).returncode,
            0,
        )
        cases = {
            "invalid.json": '{"batch_id": "B1", "events": [}',
            "duplicate.json": '{"batch_id": "B1", "batch_id": "B2", "events": []}',
            "nan.json": '{"batch_id": "B1", "events": [{"event_id": "e1", '
                        '"kind": "settle", "cash_budget_cents": NaN}]}',
            "infinity.json": '{"batch_id": "B1", "events": [{"event_id": "e1", '
                             '"kind": "settle", "cash_budget_cents": Infinity}]}',
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                path = self.dir / name
                path.write_text(text)
                output = self.dir / (name + ".report")
                result = self.run_cli(
                    "apply", "--state", str(self.state), "--batch", str(path),
                    "--output", str(output)
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse(output.exists())
        # None of the rejected batches occupied anything.
        good = self.write("good.json", batch("B1", []))
        result = self.run_cli(
            "apply", "--state", str(self.state), "--batch", str(good),
            "--output", str(self.dir / "good.report")
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_requires_subcommand(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)


# ---------------------------------------------------------------------------
# Existing command regression


class ReviewRegressionTests(unittest.TestCase):
    def test_bid_review_cli_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "decisions.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "procurement.cli",
                    "--input",
                    str(ROOT / "fixtures" / "input.json"),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["awarded"], 1)
            self.assertEqual(summary["awarded_total_cents"], 42000)


if __name__ == "__main__":
    unittest.main()
