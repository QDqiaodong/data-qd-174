"""Tests for the response-time audit.

Most checks compare the report against an independent brute-force enumeration
of *every* maximum non-crossing reconciliation, so a report that merely
happens to match one arbitrary pairing cannot pass.
"""

import copy
import itertools
import json
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from procurement.responseaudit import (
    ResponseAuditError,
    audit_responses,
    load_json_file,
    parse_json_text,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures" / "response-audit"

BASE = datetime(2026, 4, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers


def ts(minutes=0, seconds=0, *, offset_hours=0):
    instant = BASE + timedelta(minutes=minutes, seconds=seconds)
    zone = timezone(timedelta(hours=offset_hours))
    return instant.astimezone(zone).isoformat()


def review(*lots):
    """Build a minimal review.  ``lots`` items are (lot_id, vendor_id)."""
    return {
        "lots": [
            {
                "lot_id": lot_id,
                "awarded": True,
                "award_offer_id": f"O-{lot_id}",
                "award_vendor_id": vendor_id,
                "offers": [
                    {
                        "offer_id": f"O-{lot_id}",
                        "vendor_id": vendor_id,
                        "awarded": True,
                    }
                ],
            }
            for lot_id, vendor_id in lots
        ]
    }


def logs(sites, tickets=(), checkins=(), closed_through=None):
    return {
        "closed_through": closed_through or "2026-05-01T00:00:00+00:00",
        "sites": [
            {
                "lot_id": lot_id,
                "site_id": site_id,
                "sla_minutes": sla,
                "search_minutes": search,
            }
            for lot_id, site_id, sla, search in sites
        ],
        "tickets": [
            {
                "ticket_id": ticket_id,
                "lot_id": lot_id,
                "site_id": site_id,
                "opened_at": opened_at,
            }
            for ticket_id, lot_id, site_id, opened_at in tickets
        ],
        "checkins": [
            {
                "checkin_id": checkin_id,
                "lot_id": lot_id,
                "site_id": site_id,
                "vendor_id": vendor_id,
                "arrived_at": arrived_at,
            }
            for checkin_id, lot_id, site_id, vendor_id, arrived_at in checkins
        ],
    }


def one_site_report(events, sla=5, search=20):
    """Run one site whose tickets/checkins are ``(id, minute, second)``."""
    tickets, checkins = events
    return audit_responses(
        review(("L1", "V1")),
        logs(
            [("L1", "S1", sla, search)],
            [(tid, "L1", "S1", ts(minute, second)) for tid, minute, second in tickets],
            [
                (cid, "L1", "S1", "V1", ts(minute, second))
                for cid, minute, second in checkins
            ],
        ),
    )["lots"][0]["sites"][0]


def brute_force(ticket_times, checkin_times, sla, search):
    """Enumerate every maximum non-crossing matching and derive all facts."""
    n, m = len(ticket_times), len(checkin_times)
    window = timedelta(minutes=search)
    sla_window = timedelta(minutes=sla)
    adjacency = [
        [j for j in range(m) if ticket_times[i] <= checkin_times[j] <= ticket_times[i] + window]
        for i in range(n)
    ]
    schemes = []

    def visit(i, last_j, pairs):
        if i == n:
            schemes.append(tuple(pairs))
            return
        visit(i + 1, last_j, pairs)
        for j in adjacency[i]:
            if j > last_j:
                visit(i + 1, j, pairs + [(i, j)])

    visit(0, -1, [])
    max_pairs = max(len(scheme) for scheme in schemes)
    schemes = [scheme for scheme in schemes if len(scheme) == max_pairs]
    facts = []
    for i in range(n):
        partners = sorted(
            {j for scheme in schemes for ii, j in scheme if ii == i}
        )
        unmatched = any(not any(ii == i for ii, _ in scheme) for scheme in schemes)
        on_time = any(
            any(ii == i and checkin_times[j] - ticket_times[i] <= sla_window for ii, j in scheme)
            for scheme in schemes
        )
        late = any(
            any(ii == i and checkin_times[j] - ticket_times[i] > sla_window for ii, j in scheme)
            for scheme in schemes
        )
        possible = []
        if on_time:
            possible.append("on_time")
        if late:
            possible.append("late")
        if unmatched:
            possible.append("no_checkin")
        facts.append(
            {
                "possible": partners,
                "unmatched": unmatched,
                "on_time": on_time,
                "late": late,
                "possible_verdicts": possible,
            }
        )
    return max_pairs, facts


# ---------------------------------------------------------------------------
# The prompt's worked examples


class WorkedExampleTests(unittest.TestCase):
    def test_one_checkin_shared_by_two_tickets(self):
        site = one_site_report(
            ([("T1", 0, 0), ("T2", 0, 180)], [("C1", 0, 360)])
        )
        self.assertEqual(site["max_pairs"], 1)
        first, second = site["tickets"]
        self.assertEqual(first["possible_checkin_ids"], ["C1"])
        self.assertTrue(first["can_be_unmatched"])
        self.assertEqual(first["verdict"], "needs_review")
        self.assertEqual(sorted(first["possible_verdicts"]), ["late", "no_checkin"])
        self.assertIsNone(first["confirmed_checkin_id"])
        self.assertEqual(sorted(second["possible_verdicts"]), ["no_checkin", "on_time"])
        self.assertEqual(second["verdict"], "needs_review")

    def test_two_alternative_checkins_confirm_on_time_without_an_id(self):
        site = one_site_report(
            ([("T1", 0, 0)], [("C1", 0, 120), ("C2", 0, 240)])
        )
        row = site["tickets"][0]
        self.assertEqual(site["max_pairs"], 1)
        self.assertEqual(row["possible_checkin_ids"], ["C1", "C2"])
        self.assertFalse(row["can_be_unmatched"])
        self.assertIsNone(row["confirmed_checkin_id"])
        self.assertEqual(row["verdict"], "on_time")
        self.assertEqual(row["possible_verdicts"], ["on_time"])

    def test_unreachable_late_checkin_is_not_a_possible_partner(self):
        # The prompt's counter-example: 09:00 and 09:05 tickets, 09:01 and
        # 09:08 check-ins.  T1's window reaches 09:20, but pairing T1 with the
        # 09:08 check-in would strand T2 and lose the maximum of 2.
        site = one_site_report(
            (
                [("T1", 0, 0), ("T2", 0, 300)],
                [("C1", 0, 60), ("C2", 0, 480)],
            )
        )
        self.assertEqual(site["max_pairs"], 2)
        first = site["tickets"][0]
        self.assertEqual(first["possible_checkin_ids"], ["C1"])
        self.assertEqual(first["confirmed_checkin_id"], "C1")
        self.assertEqual(first["verdict"], "on_time")
        self.assertNotIn("C2", first["possible_checkin_ids"])


# ---------------------------------------------------------------------------
# Enumeration-independent properties


class BruteForceAgreementTests(unittest.TestCase):
    def run_case(self, seed):
        rnd = random.Random(seed)
        n = rnd.randint(0, 6)
        m = rnd.randint(0, 6)
        sla = rnd.randint(1, 5)
        search = rnd.randint(sla, 8)
        ticket_times = sorted(BASE + timedelta(seconds=rnd.randint(0, 40) * 15) for _ in range(n))
        checkin_times = sorted(
            BASE + timedelta(seconds=rnd.randint(-2, 45) * 15) for _ in range(m)
        )
        expected_pairs, expected = brute_force(ticket_times, checkin_times, sla, search)
        site = audit_responses(
            review(("L1", "V1")),
            logs(
                [("L1", "S1", sla, search)],
                [
                    (f"T{i:02d}", "L1", "S1", t.isoformat())
                    for i, t in enumerate(ticket_times)
                ],
                [
                    (f"C{j:02d}", "L1", "S1", "V1", t.isoformat())
                    for j, t in enumerate(checkin_times)
                ],
            ),
        )["lots"][0]["sites"][0]
        self.assertEqual(site["max_pairs"], expected_pairs)
        self.assertEqual(len(site["tickets"]), n)
        for row, facts in zip(site["tickets"], expected):
            self.assertEqual(row["possible_checkin_ids"], [f"C{j:02d}" for j in facts["possible"]])
            self.assertEqual(row["can_be_unmatched"], facts["unmatched"])
            self.assertEqual(row["possible_verdicts"], facts["possible_verdicts"])
            expected_verdict = (
                facts["possible_verdicts"][0]
                if len(facts["possible_verdicts"]) == 1
                else "needs_review"
            )
            self.assertEqual(row["verdict"], expected_verdict)
            confirmable = len(facts["possible"]) == 1 and not facts["unmatched"]
            self.assertEqual(
                row["confirmed_checkin_id"],
                f"C{facts['possible'][0]:02d}" if confirmable else None,
            )

    def test_random_small_cases_match_brute_force(self):
        for seed in range(150):
            with self.subTest(seed=seed):
                self.run_case(seed)

    def test_duplicate_instants_and_ties_match_brute_force(self):
        # Small time grid with many equal timestamps, so order-by-ID and
        # same-instant handling are exercised heavily.
        for seed in range(100, 160):
            with self.subTest(seed=seed):
                rnd = random.Random(seed)
                ticket_times = sorted(BASE + timedelta(minutes=rnd.randint(0, 3)) for _ in range(rnd.randint(0, 5)))
                checkin_times = sorted(BASE + timedelta(minutes=rnd.randint(0, 5)) for _ in range(rnd.randint(0, 5)))
                expected_pairs, expected = brute_force(ticket_times, checkin_times, 2, 4)
                site = audit_responses(
                    review(("L1", "V1")),
                    logs(
                        [("L1", "S1", 2, 4)],
                        [
                            (f"T{i:02d}", "L1", "S1", t.isoformat())
                            for i, t in enumerate(ticket_times)
                        ],
                        [
                            (f"C{j:02d}", "L1", "S1", "V1", t.isoformat())
                            for j, t in enumerate(checkin_times)
                        ],
                    ),
                )["lots"][0]["sites"][0]
                self.assertEqual(site["max_pairs"], expected_pairs)
                for row, facts in zip(site["tickets"], expected):
                    self.assertEqual(row["possible_checkin_ids"], [f"C{j:02d}" for j in facts["possible"]])
                    self.assertEqual(row["can_be_unmatched"], facts["unmatched"])
                    self.assertEqual(row["possible_verdicts"], facts["possible_verdicts"])


# ---------------------------------------------------------------------------
# Ordering constraints


class OrderingTests(unittest.TestCase):
    def test_earlier_ticket_must_take_earlier_checkin(self):
        # Both tickets could reach both check-ins, but the only maximum
        # non-crossing scheme is (T1,C1),(T2,C2); the crossing scheme is
        # forbidden.
        site = one_site_report(
            (
                [("T1", 0, 0), ("T2", 0, 120)],
                [("C1", 0, 60), ("C2", 0, 180)],
            )
        )
        self.assertEqual(site["max_pairs"], 2)
        self.assertEqual(
            [row["confirmed_checkin_id"] for row in site["tickets"]], ["C1", "C2"]
        )

    def test_crossing_scheme_is_not_reported_as_possible(self):
        site = one_site_report(
            (
                [("T1", 0, 0), ("T2", 0, 60)],
                [("C1", 0, 30), ("C2", 0, 90)],
            )
        )
        # T1 could physically reach C2, but then no non-crossing pairing with
        # T2 reaches a maximum of 2.
        self.assertEqual(site["tickets"][1]["possible_checkin_ids"], ["C2"])
        self.assertEqual(site["tickets"][0]["possible_checkin_ids"], ["C1"])

    def test_same_instant_tickets_ordered_by_ticket_id(self):
        # Two tickets at one instant are distinct facts; the ID order decides
        # which one takes the earlier check-in in the witness, but both
        # schemes remain maximum and neither partner is confirmed.
        site = one_site_report(
            (
                [("TA", 0, 0), ("TB", 0, 0)],
                [("C1", 0, 60), ("C2", 0, 120)],
            )
        )
        self.assertEqual(site["max_pairs"], 2)
        # Only one non-crossing order exists because ticket order follows the
        # IDs: TA -> C1, TB -> C2.
        self.assertEqual(site["tickets"][0]["confirmed_checkin_id"], "C1")
        self.assertEqual(site["tickets"][1]["confirmed_checkin_id"], "C2")

    def test_same_instant_checkins_ordered_by_checkin_id(self):
        site = one_site_report(
            (
                [("T1", 0, 0), ("T2", 0, 0)],
                [("CB", 0, 60), ("CA", 0, 60)],
            )
        )
        self.assertEqual(site["max_pairs"], 2)
        self.assertEqual(site["tickets"][0]["confirmed_checkin_id"], "CA")
        self.assertEqual(site["tickets"][1]["confirmed_checkin_id"], "CB")

    def test_source_array_order_does_not_change_report(self):
        events = (
            [("T1", 0, 0), ("T2", 0, 180), ("T3", 0, 240)],
            [("C1", 0, 120), ("C2", 0, 240), ("C3", 0, 300)],
        )
        baseline = one_site_report(events)
        variants = []
        for ticket_order in itertools.permutations(events[0]):
            for checkin_order in itertools.permutations(events[1]):
                variants.append((list(ticket_order), list(checkin_order)))
        for tickets, checkins in variants[:20]:
            with self.subTest(tickets=[t[0] for t in tickets]):
                self.assertEqual(one_site_report((tickets, checkins)), baseline)


# ---------------------------------------------------------------------------
# Boundaries, offsets, isolation


class BoundaryTests(unittest.TestCase):
    def test_search_window_includes_its_end(self):
        site = one_site_report(([("T1", 0, 0)], [("C1", 0, 20 * 60)]))
        self.assertEqual(site["max_pairs"], 1)
        self.assertEqual(site["tickets"][0]["confirmed_checkin_id"], "C1")

    def test_checkin_just_after_search_window_is_excluded(self):
        site = one_site_report(([("T1", 0, 0)], [("C1", 0, 20 * 60 + 1)]))
        self.assertEqual(site["max_pairs"], 0)
        self.assertEqual(site["tickets"][0]["verdict"], "no_checkin")
        self.assertEqual(site["tickets"][0]["possible_checkin_ids"], [])

    def test_checkin_before_opening_is_excluded(self):
        site = one_site_report(([("T1", 0, 0)], [("C1", 0, -1)]))
        self.assertEqual(site["max_pairs"], 0)

    def test_sla_boundary_is_on_time(self):
        site = one_site_report(([("T1", 0, 0)], [("C1", 0, 5 * 60)]))
        self.assertEqual(site["tickets"][0]["verdict"], "on_time")

    def test_one_second_past_sla_is_late(self):
        site = one_site_report(([("T1", 0, 0)], [("C1", 0, 5 * 60 + 1)]))
        self.assertEqual(site["tickets"][0]["verdict"], "late")

    def test_sla_equal_to_search(self):
        site = one_site_report(([("T1", 0, 0)], [("C1", 0, 90)]), sla=3, search=3)
        self.assertEqual(site["tickets"][0]["verdict"], "on_time")
        self.assertEqual(site["max_pairs"], 1)


class OffsetTests(unittest.TestCase):
    def test_offsets_compare_by_instant(self):
        a = audit_responses(
            review(("L1", "V1")),
            logs(
                [("L1", "S1", 5, 20)],
                [("T1", "L1", "S1", "2026-04-01T09:00:00+00:00")],
                [("C1", "L1", "S1", "V1", "2026-04-01T09:02:00+00:00")],
            ),
        )
        b = audit_responses(
            review(("L1", "V1")),
            logs(
                [("L1", "S1", 5, 20)],
                [("T1", "L1", "S1", "2026-04-01T11:00:00+02:00")],
                [("C1", "L1", "S1", "V1", "2026-04-01T04:02:00-05:00")],
            ),
        )
        self.assertEqual(a, b)
        self.assertEqual(a["lots"][0]["sites"][0]["tickets"][0]["verdict"], "on_time")

    def test_same_instant_distinct_records_are_not_deduplicated(self):
        site = one_site_report(
            (
                [("T1", 0, 0), ("T2", 0, 0)],
                [("C1", 0, 60), ("C2", 0, 60)],
            )
        )
        # Two distinct tickets and two distinct check-ins at one instant.
        self.assertEqual(site["tickets_total"], 2)
        self.assertEqual(site["checkins_total"], 2)
        self.assertEqual(site["max_pairs"], 2)

    def test_reordered_arrays_and_rewritten_offsets_are_equivalent(self):
        base = logs(
            [("L1", "S1", 5, 20)],
            [("T2", "L1", "S1", ts(3)), ("T1", "L1", "S1", ts(0))],
            [
                ("C2", "L1", "S1", "V1", ts(5)),
                ("C1", "L1", "S1", "V1", ts(2)),
            ],
        )
        rewritten = copy.deepcopy(base)
        rewritten["tickets"].reverse()
        rewritten["checkins"].reverse()
        for row in rewritten["tickets"]:
            minute = row["opened_at"][14:16]
            row["opened_at"] = ts(int(minute), offset_hours=3)
        for row in rewritten["checkins"]:
            minute = row["arrived_at"][14:16]
            row["arrived_at"] = ts(int(minute), offset_hours=-7)
        self.assertEqual(
            audit_responses(review(("L1", "V1")), base),
            audit_responses(review(("L1", "V1")), rewritten),
        )


    def test_window_end_written_with_another_offset_is_the_same_instant(self):
        result = audit_responses(
            review(("L1", "V1")),
            logs(
                [("L1", "S1", 5, 20)],
                [("T1", "L1", "S1", "2026-04-01T09:00:00+00:00")],
                [("C1", "L1", "S1", "V1", "2026-04-01T11:20:00+02:00")],
            ),
        )
        self.assertEqual(result["lots"][0]["sites"][0]["max_pairs"], 1)
        result = audit_responses(
            review(("L1", "V1")),
            logs(
                [("L1", "S1", 5, 20)],
                [("T1", "L1", "S1", "2026-04-01T09:00:00+00:00")],
                [("C1", "L1", "S1", "V1", "2026-04-01T11:20:01+02:00")],
            ),
        )
        self.assertEqual(result["lots"][0]["sites"][0]["max_pairs"], 0)


class SiteIsolationTests(unittest.TestCase):
    def test_same_site_id_under_two_lots_is_isolated(self):
        result = audit_responses(
            review(("L1", "V1"), ("L2", "V2")),
            logs(
                [
                    ("L1", "S1", 5, 20),
                    ("L2", "S1", 5, 20),
                ],
                [
                    ("T1", "L1", "S1", ts(0)),
                    ("T2", "L2", "S1", ts(0)),
                    # T2's window would overlap T1 if lots were merged.
                ],
                [
                    ("C1", "L1", "S1", "V1", ts(1)),
                    ("C2", "L2", "S1", "V2", ts(2)),
                ],
            ),
        )
        by_lot = {lot["lot_id"]: lot for lot in result["lots"]}
        self.assertEqual(by_lot["L1"]["vendor_id"], "V1")
        self.assertEqual(by_lot["L2"]["vendor_id"], "V2")
        self.assertEqual(by_lot["L1"]["sites"][0]["max_pairs"], 1)
        self.assertEqual(by_lot["L2"]["sites"][0]["max_pairs"], 1)
        self.assertEqual(
            by_lot["L1"]["sites"][0]["tickets"][0]["confirmed_checkin_id"], "C1"
        )
        self.assertEqual(
            by_lot["L2"]["sites"][0]["tickets"][0]["confirmed_checkin_id"], "C2"
        )

    def test_sites_are_reported_in_business_order(self):
        result = audit_responses(
            review(("L1", "V1"), ("L2", "V2")),
            logs([("L2", "S9", 5, 20), ("L1", "S2", 5, 20), ("L1", "S1", 5, 20)]),
        )
        self.assertEqual([lot["lot_id"] for lot in result["lots"]], ["L1", "L2"])
        self.assertEqual(
            [site["site_id"] for site in result["lots"][0]["sites"]], ["S1", "S2"]
        )


# ---------------------------------------------------------------------------
# Empty and unmatched inputs


class EmptyInputTests(unittest.TestCase):
    def test_empty_queues_are_legal(self):
        result = audit_responses(
            review(("L1", "V1")), logs([("L1", "S1", 5, 20)])
        )
        site = result["lots"][0]["sites"][0]
        self.assertEqual(site["max_pairs"], 0)
        self.assertEqual(site["tickets"], [])
        self.assertEqual(result["totals"]["tickets"], 0)

    def test_no_sites_configured(self):
        result = audit_responses(review(("L1", "V1")), logs([]))
        self.assertEqual(result["lots"], [])
        self.assertEqual(result["totals"]["max_pairs"], 0)

    def test_checkin_without_ticket_is_not_an_error(self):
        site = one_site_report(([], [("C1", 0, 60)]))
        self.assertEqual(site["checkins_total"], 1)
        self.assertEqual(site["max_pairs"], 0)

    def test_ticket_without_any_checkin_confirms_missing(self):
        site = one_site_report(([("T1", 0, 0)], []))
        row = site["tickets"][0]
        self.assertEqual(row["verdict"], "no_checkin")
        self.assertEqual(row["possible_verdicts"], ["no_checkin"])
        self.assertTrue(row["can_be_unmatched"])


# ---------------------------------------------------------------------------
# Invalid input


class InvalidInputTests(unittest.TestCase):
    def base_logs(self):
        return logs(
            [("L1", "S1", 5, 20)],
            [("T1", "L1", "S1", ts(0))],
            [("C1", "L1", "S1", "V1", ts(2))],
        )

    def test_duplicate_ticket_id(self):
        value = self.base_logs()
        value["tickets"].append(dict(value["tickets"][0]))
        with self.assertRaisesRegex(ResponseAuditError, "duplicate ticket_id"):
            audit_responses(review(("L1", "V1")), value)

    def test_duplicate_checkin_id(self):
        value = self.base_logs()
        value["checkins"].append(dict(value["checkins"][0], arrived_at=ts(3)))
        with self.assertRaisesRegex(ResponseAuditError, "duplicate checkin_id"):
            audit_responses(review(("L1", "V1")), value)

    def test_duplicate_site(self):
        value = self.base_logs()
        value["sites"].append(dict(value["sites"][0]))
        with self.assertRaisesRegex(ResponseAuditError, "duplicate site"):
            audit_responses(review(("L1", "V1")), value)

    def test_unknown_site(self):
        value = self.base_logs()
        value["tickets"][0]["site_id"] = "NOPE"
        with self.assertRaisesRegex(ResponseAuditError, "unknown site"):
            audit_responses(review(("L1", "V1")), value)

    def test_unknown_lot_in_sites(self):
        value = self.base_logs()
        value["sites"][0]["lot_id"] = "L9"
        with self.assertRaisesRegex(ResponseAuditError, "unknown review lot"):
            audit_responses(review(("L1", "V1")), value)

    def test_bool_is_not_an_integer(self):
        value = self.base_logs()
        value["sites"][0]["sla_minutes"] = True
        with self.assertRaisesRegex(ResponseAuditError, "positive integer"):
            audit_responses(review(("L1", "V1")), value)

    def test_non_positive_time_limits(self):
        for field in ("sla_minutes", "search_minutes"):
            value = self.base_logs()
            value["sites"][0][field] = 0
            with self.assertRaisesRegex(ResponseAuditError, "positive integer"):
                audit_responses(review(("L1", "V1")), value)

    def test_sla_after_search(self):
        value = self.base_logs()
        value["sites"][0]["sla_minutes"] = 21
        with self.assertRaisesRegex(ResponseAuditError, "must not exceed"):
            audit_responses(review(("L1", "V1")), value)

    def test_timestamp_without_offset(self):
        value = self.base_logs()
        value["tickets"][0]["opened_at"] = "2026-04-01T09:00:00"
        with self.assertRaisesRegex(ResponseAuditError, "requires a UTC offset"):
            audit_responses(review(("L1", "V1")), value)

    def test_invalid_timestamp(self):
        value = self.base_logs()
        value["checkins"][0]["arrived_at"] = "not-a-time"
        with self.assertRaisesRegex(ResponseAuditError, "not a valid ISO"):
            audit_responses(review(("L1", "V1")), value)

    def test_non_string_id(self):
        value = self.base_logs()
        value["tickets"][0]["ticket_id"] = 7
        with self.assertRaisesRegex(ResponseAuditError, "non-empty string"):
            audit_responses(review(("L1", "V1")), value)

    def test_empty_id(self):
        value = self.base_logs()
        value["checkins"][0]["checkin_id"] = ""
        with self.assertRaisesRegex(ResponseAuditError, "non-empty string"):
            audit_responses(review(("L1", "V1")), value)

    def test_wrong_vendor(self):
        value = self.base_logs()
        value["checkins"][0]["vendor_id"] = "V2"
        with self.assertRaisesRegex(ResponseAuditError, "not the awarded vendor"):
            audit_responses(review(("L1", "V1")), value)

    def test_ticket_window_past_closed_through(self):
        value = self.base_logs()
        value["closed_through"] = ts(10)  # ticket opens 09:00, window ends 09:20
        with self.assertRaisesRegex(ResponseAuditError, "search window ends"):
            audit_responses(review(("L1", "V1")), value)

    def test_checkin_after_closed_through(self):
        value = self.base_logs()
        value["closed_through"] = ts(1)
        with self.assertRaisesRegex(ResponseAuditError, "after logs.closed_through"):
            audit_responses(review(("L1", "V1")), value)

    def test_closed_through_includes_its_instant(self):
        value = self.base_logs()
        value["closed_through"] = ts(2)  # equal to the check-in and to window end
        value["tickets"][0]["opened_at"] = ts(-18)  # window ends exactly at ts(2)
        audit_responses(review(("L1", "V1")), value)

    def test_unawarded_lot_referenced(self):
        value = self.base_logs()
        value["sites"][0]["lot_id"] = "L2"
        unawarded = review(("L1", "V1"))
        unawarded["lots"].append(
            {
                "lot_id": "L2",
                "awarded": False,
                "award_offer_id": None,
                "award_vendor_id": None,
                "offers": [
                    {"offer_id": "O-L2", "vendor_id": "V9", "awarded": False}
                ],
            }
        )
        with self.assertRaisesRegex(ResponseAuditError, "not\nawarded|not awarded"):
            audit_responses(unawarded, value)

    def test_award_offer_mismatch(self):
        value = review(("L1", "V1"))
        value["lots"][0]["award_offer_id"] = "OTHER"
        with self.assertRaisesRegex(ResponseAuditError, "does not name"):
            audit_responses(value, self.base_logs())

    def test_award_vendor_mismatch(self):
        value = review(("L1", "V1"))
        value["lots"][0]["award_vendor_id"] = "OTHER"
        with self.assertRaisesRegex(ResponseAuditError, "does not\nmatch|does not match"):
            audit_responses(value, self.base_logs())

    def test_two_awarded_offers_on_a_lot(self):
        value = review(("L1", "V1"))
        value["lots"][0]["offers"].append(
            {"offer_id": "O2", "vendor_id": "V1", "awarded": True}
        )
        with self.assertRaisesRegex(ResponseAuditError, "exactly one"):
            audit_responses(value, self.base_logs())

    def test_window_beyond_representable_range_is_a_clean_error(self):
        value = self.base_logs()
        value["closed_through"] = "9999-12-31T00:00:00+00:00"
        value["tickets"][0]["opened_at"] = "9999-12-31T23:59:00+00:00"
        with self.assertRaisesRegex(ResponseAuditError, "representable instant range"):
            audit_responses(review(("L1", "V1")), value)

    def test_input_is_not_modified(self):
        review_value = review(("L1", "V1"))
        log_value = self.base_logs()
        review_before = copy.deepcopy(review_value)
        logs_before = copy.deepcopy(log_value)
        audit_responses(review_value, log_value)
        self.assertEqual(review_value, review_before)
        self.assertEqual(log_value, logs_before)


# ---------------------------------------------------------------------------
# Scale benchmark


class ScaleBenchmarkTests(unittest.TestCase):
    """The documented 30,000-ticket / 30,000-check-in benchmark.

    Skipped unless ``RESPONSE_AUDIT_SCALE=1``; see ``docs/response-audit.md``
    for the generation and timing procedure.  The test asserts the shape the
    performance claim depends on (at least 30,000 of each record, fewer than
    300,000 candidate edges, many interchangeable pairs) and that the run
    under a 512 MiB address-space cap finishes well inside 60 seconds.
    """

    @classmethod
    def setUpClass(cls):
        cls.review_value = load_json_file(FIXTURES / "scale-review.json")
        cls.logs_value = load_json_file(FIXTURES / "scale-logs.json")

    @unittest.skipUnless(
        __import__("os").environ.get("RESPONSE_AUDIT_SCALE") == "1",
        "set RESPONSE_AUDIT_SCALE=1 to run the 30k benchmark",
    )
    def test_scale_case_shape_and_budget(self):
        import resource
        import time

        tickets = self.logs_value["tickets"]
        checkins = self.logs_value["checkins"]
        self.assertGreaterEqual(len(tickets), 30000)
        self.assertGreaterEqual(len(checkins), 30000)

        # Count candidate edges independently of the solver.
        site = self.logs_value["sites"][0]
        window = timedelta(minutes=site["search_minutes"])
        checkin_times = sorted(
            datetime.fromisoformat(row["arrived_at"]) for row in checkins
        )
        from bisect import bisect_left, bisect_right

        edges = 0
        for row in tickets:
            opened = datetime.fromisoformat(row["opened_at"])
            edges += bisect_right(checkin_times, opened + window) - bisect_left(
                checkin_times, opened
            )
        self.assertLess(edges, 300000)

        start = time.monotonic()
        result = audit_responses(self.review_value, self.logs_value)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 60.0, f"scale audit took {elapsed:.1f}s")
        self.assertLess(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 512
        )
        audit_site = result["lots"][0]["sites"][0]
        self.assertEqual(audit_site["max_pairs"], len(tickets))
        # Interchangeability: no ticket has a single confirmable check-in.
        self.assertTrue(
            all(row["confirmed_checkin_id"] is None for row in audit_site["tickets"])
        )
        self.assertTrue(
            all(len(row["possible_checkin_ids"]) > 1 for row in audit_site["tickets"])
        )


class ReviewToleranceTests(unittest.TestCase):
    def test_unrelated_review_fields_are_tolerated(self):
        value = review(("L1", "V1"))
        value["package"] = {"budget_cents": 1, "vendors": []}
        value["qualification_enabled"] = True
        value["qualification_as_of"] = "2026-04-01T00:00:00+00:00"
        value["lots"][0]["offers"][0]["price_cents"] = 100
        value["lots"][0]["offers"][0]["eligible"] = True
        value["lots"][0]["offers"][0]["reasons"] = []
        value["lots"][0]["offers"][0]["selection_reasons"] = []
        value["lots"][0]["budget_cents"] = 100
        value["lots"][0]["service_start"] = "2026-04-01"
        value["lots"][0]["service_end"] = "2026-05-01"
        result = audit_responses(
            value,
            logs(
                [("L1", "S1", 5, 20)],
                [("T1", "L1", "S1", ts(0))],
                [("C1", "L1", "S1", "V1", ts(2))],
            ),
        )
        self.assertEqual(result["lots"][0]["vendor_id"], "V1")

    def test_unawarded_but_unreferenced_lot_is_ignored(self):
        value = review(("L1", "V1"))
        value["lots"].append(
            {
                "lot_id": "L2",
                "awarded": False,
                "award_offer_id": None,
                "award_vendor_id": None,
                "offers": [
                    {"offer_id": "O2", "vendor_id": "V9", "awarded": False}
                ],
            }
        )
        result = audit_responses(
            value,
            logs(
                [("L1", "S1", 5, 20)],
                [("T1", "L1", "S1", ts(0))],
                [("C1", "L1", "S1", "V1", ts(2))],
            ),
        )
        self.assertEqual([lot["lot_id"] for lot in result["lots"]], ["L1"])


class StrictJsonTests(unittest.TestCase):
    def test_duplicate_keys_rejected(self):
        with self.assertRaisesRegex(ResponseAuditError, "duplicate JSON object key"):
            parse_json_text('{"a": 1, "a": 2}', "sample")

    def test_non_finite_rejected(self):
        with self.assertRaisesRegex(ResponseAuditError, "non-finite"):
            parse_json_text('{"a": NaN}', "sample")

    def test_invalid_json_rejected(self):
        with self.assertRaisesRegex(ResponseAuditError, "invalid JSON"):
            parse_json_text("{", "sample")


# ---------------------------------------------------------------------------
# CLI


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name, value):
        path = self.dir / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def run_cli(self, review_path, input_path, output_path):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "procurement.response_audit_cli",
                "--review",
                str(review_path),
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_fixture_matches_expected_report(self):
        expected = load_json_file(FIXTURES / "expected-report.json")
        result = audit_responses(
            load_json_file(FIXTURES / "review.json"),
            load_json_file(FIXTURES / "logs.json"),
        )
        self.assertEqual(result, expected)

    def test_cli_writes_report_and_refuses_overwrite(self):
        output = self.dir / "report.json"
        proc = self.run_cli(
            FIXTURES / "review.json", FIXTURES / "logs.json", output
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(output.exists())
        first = output.read_text()
        proc = self.run_cli(
            FIXTURES / "review.json", FIXTURES / "logs.json", output
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("refusing to overwrite", proc.stderr)
        self.assertEqual(output.read_text(), first)

    def test_cli_invalid_input_writes_nothing(self):
        bad = self.write("bad.json", {"closed_through": "2026-05-01T00:00:00+00:00"})
        output = self.dir / "report.json"
        proc = self.run_cli(FIXTURES / "review.json", bad, output)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(output.exists())

    def test_cli_creates_parent_directories(self):
        output = self.dir / "nested" / "deep" / "report.json"
        proc = self.run_cli(
            FIXTURES / "review.json", FIXTURES / "logs.json", output
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
