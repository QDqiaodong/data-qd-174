"""Tests for supplier qualification evidence in bid review.

All data is synthetic.  Tests use only public entry points and observable
output so they remain valid if internals are reorganised.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from procurement.core import review
from procurement.qualification import QualificationError

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Builders


def credential(
    credential_id,
    revision,
    vendor_id,
    document_code,
    status,
    *,
    start="2025-01-01T00:00:00+00:00",
    end="2027-01-01T00:00:00+00:00",
    recorded="2025-01-01T00:00:00+00:00",
):
    return {
        "credential_id": credential_id,
        "revision": revision,
        "vendor_id": vendor_id,
        "document_code": document_code,
        "status": status,
        "valid_from": start,
        "valid_until": end,
        "recorded_at": recorded,
    }


def qualification(*credentials):
    return {"schema_version": 1, "credentials": list(credentials)}


def offer(offer_id, lot_id, vendor_id, price, *, submitted, documents=None):
    return {
        "id": offer_id,
        "lot_id": lot_id,
        "vendor_id": vendor_id,
        "submitted": submitted,
        "price_cents": price,
        "documents": ["doc"] if documents is None else documents,
    }


def lot(lot_id, *, deadline="2026-03-10T17:00:00+00:00", budget=100000, documents=None):
    return {
        "id": lot_id,
        "deadline": deadline,
        "budget_cents": budget,
        "required_documents": ["doc"] if documents is None else documents,
        "service_start": "2026-04-01",
        "service_end": "2026-05-01",
    }


def vendor(vendor_id, capacity=1):
    return {"id": vendor_id, "concurrent_lot_capacity": capacity}


def package(lots, offers, vendors, budget=100000):
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


def evidence(row, code):
    return next(
        item for item in row["qualification_evidence"] if item["document_code"] == code
    )


def simple_package(*, price=100, documents=None, submitted="2026-03-01T10:00:00+00:00"):
    return package(
        [lot("L1")],
        [offer("O1", "L1", "V1", price, submitted=submitted, documents=documents)],
        [vendor("V1")],
    )


class ValidityWindowTests(unittest.TestCase):
    def test_left_boundary_is_inclusive(self):
        result = review(
            simple_package(submitted="2026-03-01T00:00:00+00:00"),
            qualification(
                credential(
                    "C1",
                    1,
                    "V1",
                    "doc",
                    "active",
                    start="2026-03-01T00:00:00+00:00",
                    end="2026-04-01T00:00:00+00:00",
                    recorded="2025-01-01T00:00:00+00:00",
                )
            ),
            "2026-03-05T00:00:00+00:00",
        )
        row = offer_row(result, "L1", "O1")
        self.assertTrue(row["eligible"])
        self.assertTrue(evidence(row, "doc")["verified"])

    def test_right_boundary_is_exclusive(self):
        result = review(
            simple_package(submitted="2026-03-05T00:00:00+00:00"),
            qualification(
                credential(
                    "C1",
                    1,
                    "V1",
                    "doc",
                    "active",
                    start="2026-03-01T00:00:00+00:00",
                    end="2026-03-05T00:00:00+00:00",
                    recorded="2025-01-01T00:00:00+00:00",
                )
            ),
            "2026-03-05T00:00:00+00:00",
        )
        row = offer_row(result, "L1", "O1")
        self.assertFalse(row["eligible"])
        self.assertEqual(row["reasons"], ["unverified_documents:doc"])
        entry = evidence(row, "doc")
        self.assertFalse(entry["verified"])
        self.assertEqual(entry["reason"], "outside_validity")

    def test_instant_after_right_boundary_is_not_valid(self):
        result = review(
            simple_package(submitted="2026-03-05T00:00:01+00:00"),
            qualification(
                credential(
                    "C1",
                    1,
                    "V1",
                    "doc",
                    "active",
                    start="2026-03-01T00:00:00+00:00",
                    end="2026-03-05T00:00:00+00:00",
                    recorded="2025-01-01T00:00:00+00:00",
                )
            ),
            "2026-03-05T00:00:00+00:00",
        )
        self.assertFalse(offer_row(result, "L1", "O1")["eligible"])

    def test_equal_instants_in_different_offsets_compare_equal(self):
        # 2026-03-01T10:00:00+02:00 is 08:00Z, inside [08:00Z, 09:00Z).
        result = review(
            simple_package(submitted="2026-03-01T10:00:00+02:00"),
            qualification(
                credential(
                    "C1",
                    1,
                    "V1",
                    "doc",
                    "active",
                    start="2026-03-01T08:00:00+00:00",
                    end="2026-03-01T09:00:00+00:00",
                    recorded="2025-01-01T00:00:00+00:00",
                )
            ),
            "2026-03-01T09:30:00+01:00",  # 08:30Z
        )
        row = offer_row(result, "L1", "O1")
        self.assertTrue(row["eligible"], row["reasons"])
        self.assertEqual(evidence(row, "doc")["credential_id"], "C1")

    def test_offset_equivalence_at_right_edge_is_exclusive(self):
        # 2026-03-01T10:00:00+02:00 == 08:00Z, the exclusive right edge.
        result = review(
            simple_package(submitted="2026-03-01T10:00:00+02:00"),
            qualification(
                credential(
                    "C1",
                    1,
                    "V1",
                    "doc",
                    "active",
                    start="2026-03-01T06:00:00+00:00",
                    end="2026-03-01T08:00:00+00:00",
                    recorded="2025-01-01T00:00:00+00:00",
                )
            ),
            "2026-03-01T09:00:00+01:00",  # 08:00Z
        )
        self.assertFalse(offer_row(result, "L1", "O1")["eligible"])


class EvidenceTests(unittest.TestCase):
    def test_no_credential_is_unverified(self):
        result = review(
            simple_package(),
            qualification(),
            "2026-03-05T00:00:00+00:00",
        )
        row = offer_row(result, "L1", "O1")
        self.assertFalse(row["eligible"])
        self.assertEqual(row["reasons"], ["unverified_documents:doc"])
        entry = evidence(row, "doc")
        self.assertEqual(
            entry, {"document_code": "doc", "verified": False, "reason": "no_credential"}
        )
        self.assertNotIn("credential_id", entry)

    def test_other_vendor_credential_does_not_prove_offer(self):
        result = review(
            package(
                [lot("L1")],
                [offer("O1", "L1", "V1", 100, submitted="2026-03-01T10:00:00+00:00")],
                [vendor("V1"), vendor("V2")],
            ),
            qualification(
                credential(
                    "C1", 1, "V2", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                )
            ),
            "2026-03-05T00:00:00+00:00",
        )
        row = offer_row(result, "L1", "O1")
        self.assertFalse(row["eligible"])
        self.assertEqual(evidence(row, "doc")["reason"], "no_credential")

    def test_undeclared_required_code_reports_missing_not_unverified(self):
        result = review(
            simple_package(documents=[]),
            qualification(
                credential(
                    "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                )
            ),
            "2026-03-05T00:00:00+00:00",
        )
        row = offer_row(result, "L1", "O1")
        self.assertFalse(row["eligible"])
        self.assertEqual(row["reasons"], ["missing_documents:doc"])
        self.assertEqual(row["qualification_evidence"], [])

    def test_non_required_declared_code_is_ignored(self):
        result = review(
            simple_package(documents=["doc", "extra"]),
            qualification(
                credential(
                    "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                )
            ),
            "2026-03-05T00:00:00+00:00",
        )
        row = offer_row(result, "L1", "O1")
        self.assertTrue(row["eligible"])
        self.assertEqual(
            [item["document_code"] for item in row["qualification_evidence"]], ["doc"]
        )

    def test_smallest_credential_id_is_selected_stably(self):
        document = qualification(
            credential(
                "C-B", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
            ),
            credential(
                "C-A", 1, "V1", "doc", "active", recorded="2025-02-01T00:00:00+00:00"
            ),
        )
        first = review(simple_package(), document, "2026-03-05T00:00:00+00:00")
        second = review(
            simple_package(),
            {"schema_version": 1, "credentials": list(reversed(document["credentials"]))},
            "2026-03-05T00:00:00+00:00",
        )
        for result in (first, second):
            entry = evidence(offer_row(result, "L1", "O1"), "doc")
            self.assertEqual(entry["credential_id"], "C-A")
            self.assertEqual(entry["revision"], 1)
            self.assertEqual(entry["recorded_at"], "2025-02-01T00:00:00+00:00")

    def test_evidence_is_sorted_by_required_code(self):
        result = review(
            package(
                [lot("L1", documents=["tax", "insurance"])],
                [
                    offer(
                        "O1",
                        "L1",
                        "V1",
                        100,
                        submitted="2026-03-01T10:00:00+00:00",
                        documents=["tax", "insurance"],
                    )
                ],
                [vendor("V1")],
            ),
            qualification(
                credential(
                    "C1", 1, "V1", "insurance", "active", recorded="2025-01-01T00:00:00+00:00"
                ),
                credential(
                    "C2", 1, "V1", "tax", "active", recorded="2025-01-01T00:00:00+00:00"
                ),
            ),
            "2026-03-05T00:00:00+00:00",
        )
        codes = [
            item["document_code"]
            for item in offer_row(result, "L1", "O1")["qualification_evidence"]
        ]
        self.assertEqual(codes, ["insurance", "tax"])


class RevisionHistoryTests(unittest.TestCase):
    def test_retroactive_correction_before_as_of_changes_eligibility(self):
        # Revision 1 is active but revision 2 retroactively narrows validity so
        # it no longer covers the submission.  The correction is recorded
        # before the review instant, so the historical result must change.
        document = qualification(
            credential(
                "C1",
                1,
                "V1",
                "doc",
                "active",
                start="2025-01-01T00:00:00+00:00",
                end="2027-01-01T00:00:00+00:00",
                recorded="2025-01-01T00:00:00+00:00",
            ),
            credential(
                "C1",
                2,
                "V1",
                "doc",
                "active",
                start="2026-03-02T00:00:00+00:00",
                end="2027-01-01T00:00:00+00:00",
                recorded="2026-03-04T00:00:00+00:00",
            ),
        )
        before = review(simple_package(), document, "2026-03-03T00:00:00+00:00")
        after = review(simple_package(), document, "2026-03-05T00:00:00+00:00")
        self.assertTrue(offer_row(before, "L1", "O1")["eligible"])
        self.assertFalse(offer_row(after, "L1", "O1")["eligible"])
        self.assertEqual(
            evidence(offer_row(after, "L1", "O1"), "doc")["reason"],
            "outside_validity",
        )

    def test_revision_after_as_of_does_not_change_history(self):
        document = qualification(
            credential(
                "C1",
                1,
                "V1",
                "doc",
                "active",
                start="2025-01-01T00:00:00+00:00",
                end="2027-01-01T00:00:00+00:00",
                recorded="2025-01-01T00:00:00+00:00",
            ),
            credential(
                "C1",
                2,
                "V1",
                "doc",
                "revoked",
                start="2025-01-01T00:00:00+00:00",
                end="2027-01-01T00:00:00+00:00",
                recorded="2026-03-06T00:00:00+00:00",
            ),
        )
        historical = review(simple_package(), document, "2026-03-05T00:00:00+00:00")
        row = offer_row(historical, "L1", "O1")
        self.assertTrue(row["eligible"])
        entry = evidence(row, "doc")
        self.assertEqual(entry["revision"], 1)
        self.assertEqual(entry["recorded_at"], "2025-01-01T00:00:00+00:00")

    def test_revision_recorded_exactly_at_as_of_is_visible(self):
        document = qualification(
            credential(
                "C1", 1, "V1", "doc", "active", recorded="2026-03-05T00:00:00+00:00"
            )
        )
        row = offer_row(
            review(simple_package(), document, "2026-03-05T00:00:00+00:00"),
            "L1",
            "O1",
        )
        self.assertTrue(row["eligible"])
        self.assertEqual(evidence(row, "doc")["recorded_at"], "2026-03-05T00:00:00+00:00")

    def test_no_visible_revision_reports_no_credential(self):
        document = qualification(
            credential(
                "C1", 1, "V1", "doc", "active", recorded="2026-06-01T00:00:00+00:00"
            )
        )
        row = offer_row(
            review(simple_package(), document, "2026-03-05T00:00:00+00:00"),
            "L1",
            "O1",
        )
        self.assertFalse(row["eligible"])
        self.assertEqual(evidence(row, "doc")["reason"], "no_credential")

    def test_suspended_does_not_fall_back_to_earlier_active(self):
        document = qualification(
            credential(
                "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
            ),
            credential(
                "C1", 2, "V1", "doc", "suspended", recorded="2026-03-02T00:00:00+00:00"
            ),
        )
        row = offer_row(
            review(simple_package(), document, "2026-03-05T00:00:00+00:00"),
            "L1",
            "O1",
        )
        self.assertFalse(row["eligible"])
        entry = evidence(row, "doc")
        self.assertEqual(entry["reason"], "credential_suspended")
        self.assertNotIn("credential_id", entry)

    def test_revoked_does_not_fall_back_to_earlier_active(self):
        document = qualification(
            credential(
                "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
            ),
            credential(
                "C1", 2, "V1", "doc", "revoked", recorded="2026-03-02T00:00:00+00:00"
            ),
        )
        row = offer_row(
            review(simple_package(), document, "2026-03-05T00:00:00+00:00"),
            "L1",
            "O1",
        )
        self.assertFalse(row["eligible"])
        self.assertEqual(evidence(row, "doc")["reason"], "credential_revoked")

    def test_restored_active_revision_is_visible_again(self):
        document = qualification(
            credential(
                "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
            ),
            credential(
                "C1", 2, "V1", "doc", "suspended", recorded="2026-03-02T00:00:00+00:00"
            ),
            credential(
                "C1", 3, "V1", "doc", "active", recorded="2026-03-06T00:00:00+00:00"
            ),
        )
        suspended = review(simple_package(), document, "2026-03-05T00:00:00+00:00")
        restored = review(simple_package(), document, "2026-03-07T00:00:00+00:00")
        self.assertFalse(offer_row(suspended, "L1", "O1")["eligible"])
        row = offer_row(restored, "L1", "O1")
        self.assertTrue(row["eligible"])
        self.assertEqual(evidence(row, "doc")["revision"], 3)


class AwardInteractionTests(unittest.TestCase):
    def test_qualification_failure_changes_award_combination(self):
        # V1 is cheaper, but its credential was suspended before the review
        # instant, so the other eligible offer must win the lot.
        data = package(
            [lot("L1", budget=1000)],
            [
                offer("O1", "L1", "V1", 10, submitted="2026-03-01T10:00:00+00:00"),
                offer("O2", "L1", "V2", 40, submitted="2026-03-01T10:00:00+00:00"),
            ],
            [vendor("V1"), vendor("V2")],
            budget=1000,
        )
        document = qualification(
            credential(
                "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
            ),
            credential(
                "C1", 2, "V1", "doc", "suspended", recorded="2026-02-15T00:00:00+00:00"
            ),
            credential(
                "C2", 1, "V2", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
            ),
        )
        result = review(data, document, "2026-03-05T00:00:00+00:00")
        self.assertEqual(by_lot(result, "L1")["award_offer_id"], "O2")
        self.assertEqual(result["package"]["awarded_total_cents"], 40)
        cheap = offer_row(result, "L1", "O1")
        self.assertFalse(cheap["eligible"])
        self.assertFalse(cheap["awarded"])
        self.assertEqual(cheap["selection_reasons"], [])
        # Ineligible offers never consume budget or capacity.
        self.assertEqual(
            next(
                item
                for item in result["package"]["vendors"]
                if item["vendor_id"] == "V1"
            )["peak_concurrent_lots"],
            0,
        )

    def test_eligible_offer_blocked_by_budget_keeps_qualification_proof(self):
        data = package(
            [lot("L1", budget=1000)],
            [offer("O1", "L1", "V1", 900, submitted="2026-03-01T10:00:00+00:00")],
            [vendor("V1")],
            budget=100,
        )
        result = review(
            data,
            qualification(
                credential(
                    "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                )
            ),
            "2026-03-05T00:00:00+00:00",
        )
        row = offer_row(result, "L1", "O1")
        self.assertTrue(row["eligible"])
        self.assertFalse(row["awarded"])
        self.assertEqual(row["selection_reasons"], ["package_budget"])
        self.assertTrue(evidence(row, "doc")["verified"])

    def test_top_level_records_qualification_settings(self):
        result = review(
            simple_package(),
            qualification(
                credential(
                    "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                )
            ),
            "2026-03-05T00:00:00+00:00",
        )
        self.assertTrue(result["qualification_enabled"])
        self.assertEqual(result["qualification_as_of"], "2026-03-05T00:00:00+00:00")


class QualificationValidationTests(unittest.TestCase):
    def review_with(self, document, as_of="2026-03-05T00:00:00+00:00"):
        data = simple_package()
        data["vendors"].append(vendor("V2"))
        return review(data, document, as_of)

    def test_requires_both_arguments(self):
        with self.assertRaisesRegex(QualificationError, "together"):
            review(simple_package(), qualification(), None)
        with self.assertRaisesRegex(QualificationError, "together"):
            review(simple_package(), None, "2026-03-05T00:00:00+00:00")

    def test_rejects_bad_schema(self):
        for schema in (2, "1", True, None):
            with self.subTest(schema=schema):
                with self.assertRaisesRegex(QualificationError, "schema_version"):
                    self.review_with({"schema_version": schema, "credentials": []})

    def test_rejects_missing_and_extra_document_fields(self):
        with self.assertRaisesRegex(QualificationError, "missing"):
            self.review_with({"credentials": []})
        with self.assertRaisesRegex(QualificationError, "unknown"):
            self.review_with({"schema_version": 1, "credentials": [], "extra": 1})

    def test_rejects_missing_and_extra_credential_fields(self):
        record = credential(
            "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
        )
        missing = dict(record)
        del missing["status"]
        with self.assertRaisesRegex(QualificationError, "status"):
            self.review_with(qualification(missing))
        extra = dict(record)
        extra["note"] = "x"
        with self.assertRaisesRegex(QualificationError, "note"):
            self.review_with(qualification(extra))

    def test_rejects_duplicate_revisions_and_gaps(self):
        with self.assertRaisesRegex(QualificationError, "duplicate revision 1"):
            self.review_with(
                qualification(
                    credential(
                        "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                    ),
                    credential(
                        "C1", 1, "V1", "doc", "active", recorded="2025-02-01T00:00:00+00:00"
                    ),
                )
            )
        with self.assertRaisesRegex(QualificationError, "start at revision 1"):
            self.review_with(
                qualification(
                    credential(
                        "C1", 2, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                    )
                )
            )
        with self.assertRaisesRegex(QualificationError, "missing revision 2"):
            self.review_with(
                qualification(
                    credential(
                        "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                    ),
                    credential(
                        "C1", 3, "V1", "doc", "active", recorded="2025-03-01T00:00:00+00:00"
                    ),
                )
            )

    def test_rejects_identity_change_between_revisions(self):
        with self.assertRaisesRegex(QualificationError, "changes document_code"):
            self.review_with(
                qualification(
                    credential(
                        "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                    ),
                    credential(
                        "C1", 2, "V1", "other", "active", recorded="2025-02-01T00:00:00+00:00"
                    ),
                )
            )
        with self.assertRaisesRegex(QualificationError, "changes vendor_id"):
            self.review_with(
                qualification(
                    credential(
                        "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                    ),
                    credential(
                        "C1", 2, "V2", "doc", "active", recorded="2025-02-01T00:00:00+00:00"
                    ),
                )
            )

    def test_rejects_recorded_at_regression(self):
        with self.assertRaisesRegex(QualificationError, "recorded_at"):
            self.review_with(
                qualification(
                    credential(
                        "C1", 1, "V1", "doc", "active", recorded="2025-02-01T00:00:00+00:00"
                    ),
                    credential(
                        "C1", 2, "V1", "doc", "active", recorded="2025-02-01T00:00:00+00:00"
                    ),
                )
            )

    def test_rejects_invalid_timestamps(self):
        for field in ("valid_from", "valid_until", "recorded_at"):
            for value in ("2026-01-01T00:00:00", "not-a-time", 5):
                with self.subTest(field=field, value=value):
                    record = credential(
                        "C1",
                        1,
                        "V1",
                        "doc",
                        "active",
                        recorded="2025-01-01T00:00:00+00:00",
                    )
                    record[field] = value
                    with self.assertRaisesRegex(
                        QualificationError, f"revision 1 {field}"
                    ):
                        self.review_with(qualification(record))

    def test_rejects_invalid_validity_interval(self):
        with self.assertRaisesRegex(QualificationError, "valid_from must be before"):
            self.review_with(
                qualification(
                    credential(
                        "C1",
                        1,
                        "V1",
                        "doc",
                        "active",
                        start="2026-01-01T00:00:00+00:00",
                        end="2026-01-01T00:00:00+00:00",
                        recorded="2025-01-01T00:00:00+00:00",
                    )
                )
            )

    def test_rejects_unknown_vendor_and_status(self):
        with self.assertRaisesRegex(QualificationError, "declared vendor"):
            self.review_with(
                qualification(
                    credential(
                        "C1", 1, "V9", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
                    )
                )
            )
        with self.assertRaisesRegex(QualificationError, "status"):
            self.review_with(
                qualification(
                    credential(
                        "C1", 1, "V1", "doc", "expired", recorded="2025-01-01T00:00:00+00:00"
                    )
                )
            )

    def test_rejects_invalid_as_of(self):
        for value in ("2026-03-05T00:00:00", "nope", 3):
            with self.subTest(value=value):
                with self.assertRaisesRegex(QualificationError, "qualification_as_of"):
                    self.review_with(qualification(), as_of=value)


class AwardCombinationTests(unittest.TestCase):
    def test_disqualification_frees_budget_for_fewer_lots(self):
        # Without qualification: L1 at 60 plus L2 at 40 exactly fits 100 and
        # awards two lots.  Once V1's cheaper L1 credential is suspended, L1
        # can only cost 90, so L2 alone wins on lower total for one lot.
        data = package(
            [
                lot("L1", budget=1000),
                lot("L2", budget=1000),
            ],
            [
                offer("O1", "L1", "V1", 60, submitted="2026-03-01T10:00:00+00:00"),
                offer("O2", "L1", "V2", 90, submitted="2026-03-01T10:00:00+00:00"),
                offer("O3", "L2", "V3", 40, submitted="2026-03-01T10:00:00+00:00"),
            ],
            [vendor("V1"), vendor("V2"), vendor("V3")],
            budget=100,
        )
        document = qualification(
            credential(
                "C1", 1, "V1", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
            ),
            credential(
                "C1", 2, "V1", "doc", "revoked", recorded="2026-02-15T00:00:00+00:00"
            ),
            credential(
                "C2", 1, "V2", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
            ),
            credential(
                "C3", 1, "V3", "doc", "active", recorded="2025-01-01T00:00:00+00:00"
            ),
        )
        legacy = review(data)
        self.assertEqual(legacy["awarded"], 2)
        self.assertEqual(legacy["package"]["awarded_total_cents"], 100)

        checked = review(data, document, "2026-03-05T00:00:00+00:00")
        self.assertEqual(checked["awarded"], 1)
        self.assertIsNone(by_lot(checked, "L1")["award_offer_id"])
        self.assertEqual(by_lot(checked, "L2")["award_offer_id"], "O3")
        self.assertEqual(checked["package"]["awarded_total_cents"], 40)
        self.assertFalse(offer_row(checked, "L1", "O1")["eligible"])


class CliQualificationTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "procurement.cli", *map(str, args)],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    def test_fixture_award_depends_on_review_instant(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = [
                "--input",
                ROOT / "fixtures/qualification/package.json",
                "--qualification-file",
                ROOT / "fixtures/qualification/qualification.json",
            ]
            before = Path(tmp) / "before.json"
            run = self.run_cli(
                *base,
                "--qualification-as-of",
                "2026-03-05T00:00:00+00:00",
                "--output",
                before,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(before.read_text())
            self.assertEqual(result["lots"][0]["award_offer_id"], "O2")
            self.assertEqual(result["qualification_as_of"], "2026-03-05T00:00:00+00:00")

            after = Path(tmp) / "after.json"
            run = self.run_cli(
                *base,
                "--qualification-as-of",
                "2026-03-07T00:00:00+00:00",
                "--output",
                after,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(
                json.loads(after.read_text())["lots"][0]["award_offer_id"], "O1"
            )

    def test_requires_both_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.json"
            run = self.run_cli(
                "--input",
                ROOT / "fixtures/input.json",
                "--qualification-file",
                ROOT / "fixtures/qualification/qualification.json",
                "--output",
                output,
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("together", run.stderr)
            self.assertFalse(output.exists())

    def test_malformed_qualification_makes_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"schema_version": 9, "credentials": []}))
            output = Path(tmp) / "out.json"
            run = self.run_cli(
                "--input",
                ROOT / "fixtures/input.json",
                "--qualification-file",
                bad,
                "--qualification-as-of",
                "2026-03-05T00:00:00+00:00",
                "--output",
                output,
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("schema_version", run.stderr)
            self.assertFalse(output.exists())

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.json"
            output.write_text("keep me")
            run = self.run_cli(
                "--input",
                ROOT / "fixtures/qualification/package.json",
                "--qualification-file",
                ROOT / "fixtures/qualification/qualification.json",
                "--qualification-as-of",
                "2026-03-05T00:00:00+00:00",
                "--output",
                output,
            )
            self.assertEqual(run.returncode, 2)
            self.assertEqual(output.read_text(), "keep me")

    def test_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(
                '{"schema_version": 1, "credentials": [], "credentials": []}'
            )
            output = Path(tmp) / "out.json"
            run = self.run_cli(
                "--input",
                ROOT / "fixtures/input.json",
                "--qualification-file",
                bad,
                "--qualification-as-of",
                "2026-03-05T00:00:00+00:00",
                "--output",
                output,
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("duplicate JSON object key", run.stderr)
            self.assertFalse(output.exists())

    def test_rejects_non_finite_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"schema_version": 1, "credentials": [], "x": NaN}')
            output = Path(tmp) / "out.json"
            run = self.run_cli(
                "--input",
                ROOT / "fixtures/input.json",
                "--qualification-file",
                bad,
                "--qualification-as-of",
                "2026-03-05T00:00:00+00:00",
                "--output",
                output,
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("non-finite", run.stderr)
            self.assertFalse(output.exists())

    def test_as_of_requires_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.json"
            run = self.run_cli(
                "--input",
                ROOT / "fixtures/input.json",
                "--qualification-file",
                ROOT / "fixtures/qualification/qualification.json",
                "--qualification-as-of",
                "2026-03-05T00:00:00",
                "--output",
                output,
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("offset", run.stderr)
            self.assertFalse(output.exists())

    def test_legacy_fixture_without_qualification_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.json"
            run = self.run_cli(
                "--input", ROOT / "fixtures/input.json", "--output", output
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(output.read_text())
            self.assertNotIn("qualification_enabled", result)
            self.assertNotIn("qualification_as_of", result)
            self.assertNotIn("qualification_evidence", result["lots"][0]["offers"][0])
            self.assertEqual(result["awarded"], 1)
            self.assertEqual(result["package"]["awarded_total_cents"], 42000)


if __name__ == "__main__":
    unittest.main()
