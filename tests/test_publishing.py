"""Tests for paged mirror publication and independent page reception.

The assertions describe observable publication behaviour: page boundaries
always stay query-valid, limits are honoured, reception needs only the page
plus the current mirror, and identical inputs always produce identical
bundles regardless of array or key ordering.
"""

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from procurement.publishing import (
    PublishError,
    apply_page,
    build_bundle,
    canonical_mirror_bytes,
    parse_json_text,
    mirror_sha256,
    validate_mirror,
    validate_release,
    _apply_operations,
    _validate_operation,
)
from procurement.core import review

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Builders
#


def supplier(vendor_id, public_ref):
    return {"vendor_id": vendor_id, "public_ref": public_ref}


def award(package_id, lot_id, offer_id, vendor_id, price_cents):
    return {
        "package_id": package_id,
        "lot_id": lot_id,
        "offer_id": offer_id,
        "vendor_id": vendor_id,
        "price_cents": price_cents,
    }


def awarded_lot(lot_id, offer_id, vendor_id, price_cents, *, extra_offer=False):
    offers = [
        {
            "offer_id": offer_id,
            "vendor_id": vendor_id,
            "price_cents": price_cents,
            "awarded": True,
        }
    ]
    if extra_offer:
        offers.append(
            {
                "offer_id": offer_id + "-LOSE",
                "vendor_id": vendor_id,
                "price_cents": price_cents + 1,
                "awarded": False,
                "selection_reasons": ["objective_tradeoff"],
            }
        )
    return {
        "lot_id": lot_id,
        "award_offer_id": offer_id,
        "award_vendor_id": vendor_id,
        "budget_cents": 9999,
        "offers": offers,
    }


def unawarded_lot(lot_id, vendor_id="V1"):
    return {
        "lot_id": lot_id,
        "award_offer_id": None,
        "award_vendor_id": None,
        "offers": [
            {
                "offer_id": lot_id + "-O",
                "vendor_id": vendor_id,
                "price_cents": 7,
                "awarded": False,
            }
        ],
    }


def make_mirror(stream_id, position, suppliers, awards):
    return {
        "schema_version": 1,
        "stream_id": stream_id,
        "position": position,
        "suppliers": suppliers,
        "awards": awards,
    }


def make_release(stream_id, suppliers, packages):
    return {
        "schema_version": 1,
        "stream_id": stream_id,
        "suppliers": suppliers,
        "packages": [
            {"package_id": package_id, "result": {"lots": lots}}
            for package_id, lots in packages
        ],
    }


def target_rows(release):
    target = validate_release(release)
    return (
        [target["suppliers"][key] for key in sorted(target["suppliers"])],
        [target["awards"][key] for key in sorted(target["awards"])],
    )


def apply_chain(mirror, bundle, max_ops):
    """Apply every page in order and return the final mirror."""
    current = mirror
    for index, page in enumerate(bundle["pages"]):
        current = apply_page(current, page, max_ops)
        self_check_page_chain(bundle, index, current)
    return current


def self_check_page_chain(bundle, index, current):
    page = bundle["pages"][index]
    assert current["position"] == page["from_position"] + 1
    assert mirror_sha256(validate_mirror(current)) == page["after_sha256"]


def handover_base():
    """The spec's worked handover example."""
    return make_mirror(
        "STREAM-1",
        0,
        [supplier("S1", "R1"), supplier("S3", "R3")],
        [
            award("P1", "L1", "O1", "S1", 100),
            award("P1", "L2", "O2", "S1", 200),
            award("P2", "L9", "O9", "S3", 500),
        ],
    )


def handover_release(independent_price=450):
    return make_release(
        "STREAM-1",
        [supplier("S2", "R1"), supplier("S3", "R3")],
        [
            (
                "P1",
                [
                    awarded_lot("L1", "N1", "S2", 100),
                    awarded_lot("L2", "N2", "S2", 200),
                ],
            ),
            ("P2", [awarded_lot("L9", "O9", "S3", independent_price)]),
        ],
    )


class PublicationPlanningTests(unittest.TestCase):
    def test_handover_fits_at_four_and_reconstructs_target(self):
        base = handover_base()
        release = handover_release()
        bundle = build_bundle(base, release, 4)
        sizes = [len(page["operations"]) for page in bundle["pages"]]
        self.assertEqual(sizes, [4, 1])
        final = apply_chain(base, bundle, 4)
        suppliers, awards = target_rows(release)
        self.assertEqual(final["suppliers"], suppliers)
        self.assertEqual(final["awards"], awards)
        self.assertEqual(final["position"], 2)

    def test_handover_just_fits_at_four_even_with_independent_change(self):
        # The independent price correction must not be swallowed into the
        # four-operation constrained component.
        bundle = build_bundle(handover_base(), handover_release(), 4)
        first_page = bundle["pages"][0]
        keys = {
            (op["table"], json.dumps(op["key"]))
            for op in first_page["operations"]
        }
        self.assertEqual(len(keys), 4)
        self.assertTrue(all(len(page["operations"]) <= 4 for page in bundle["pages"]))

    def test_handover_must_fail_when_component_exceeds_limit(self):
        with self.assertRaises(PublishError) as caught:
            build_bundle(handover_base(), handover_release(), 3)
        message = str(caught.exception)
        self.assertIn("max-ops 3", message)
        # The error names the mutually constrained records.
        for label in ("S1", "S2", "P1", "L1", "L2"):
            self.assertIn(label, message)
        self.assertNotIn("L9", message)

    def test_public_ref_swap_must_be_one_page(self):
        base = make_mirror(
            "T", 0,
            [supplier("A", "R1"), supplier("B", "R2")],
            [award("P", "L1", "O1", "A", 10)],
        )
        release = make_release(
            "T",
            [supplier("A", "R2"), supplier("B", "R1")],
            [("P", [awarded_lot("L1", "O1", "A", 10)])],
        )
        bundle = build_bundle(base, release, 2)
        self.assertEqual(len(bundle["pages"]), 1)
        self.assertEqual(len(bundle["pages"][0]["operations"]), 2)
        final = apply_chain(base, bundle, 2)
        # Awards keyed on the stable vendor identity are untouched.
        self.assertEqual(final["awards"], [award("P", "L1", "O1", "A", 10)])
        with self.assertRaises(PublishError):
            build_bundle(base, release, 1)

    def test_one_way_dependencies_split_across_pages(self):
        base = make_mirror("T", 0, [supplier("V1", "R1")], [])
        release = make_release(
            "T",
            [supplier("V1", "R1"), supplier("V2", "R2")],
            [("P", [awarded_lot("L1", "O2", "V2", 5)])],
        )
        bundle = build_bundle(base, release, 1)
        self.assertEqual(len(bundle["pages"]), 2)
        self.assertEqual(
            bundle["pages"][0]["operations"][0]["table"], "suppliers"
        )
        self.assertEqual(
            bundle["pages"][1]["operations"][0]["table"], "awards"
        )
        final = apply_chain(base, bundle, 1)
        self.assertEqual(final["position"], 2)

    def test_release_then_acquire_without_awards_is_splittable(self):
        base = make_mirror("T", 0, [supplier("S1", "R1")], [])
        release = make_release("T", [supplier("S2", "R1")], [])
        bundle = build_bundle(base, release, 1)
        self.assertEqual(len(bundle["pages"]), 2)
        self.assertEqual(bundle["pages"][0]["operations"][0]["action"], "delete")
        self.assertEqual(bundle["pages"][1]["operations"][0]["action"], "upsert")
        apply_chain(base, bundle, 1)

    def test_cross_package_same_lot_id_does_not_collide(self):
        base = make_mirror(
            "T", 0, [supplier("V1", "R1")], [award("P1", "L1", "O1", "V1", 10)]
        )
        release = make_release(
            "T",
            [supplier("V1", "R1")],
            [
                ("P1", [awarded_lot("L1", "O1", "V1", 11)]),
                ("P2", [awarded_lot("L1", "O2", "V1", 22)]),
            ],
        )
        bundle = build_bundle(base, release, 5)
        final = apply_chain(base, bundle, 5)
        self.assertEqual(
            {(row["package_id"], row["lot_id"]) for row in final["awards"]},
            {("P1", "L1"), ("P2", "L1")},
        )

    def test_no_changes_emits_no_pages(self):
        base = handover_base()
        release = handover_release(independent_price=500)
        # Align the base awards exactly with what the release projects.
        base["awards"] = [
            award("P1", "L1", "N1", "S2", 100),
            award("P1", "L2", "N2", "S2", 200),
            award("P2", "L9", "O9", "S3", 500),
        ]
        base["suppliers"] = [supplier("S2", "R1"), supplier("S3", "R3")]
        bundle = build_bundle(base, release, 4)
        self.assertEqual(bundle["pages"], [])

    def test_award_withdrawal_and_vanished_package(self):
        base = make_mirror(
            "T",
            0,
            [supplier("V1", "R1")],
            [award("P1", "L1", "O1", "V1", 10), award("P2", "L1", "O2", "V1", 20)],
        )
        release = make_release(
            "T", [supplier("V1", "R1")], [("P1", [unawarded_lot("L1")])]
        )
        bundle = build_bundle(base, release, 5)
        operations = [op for page in bundle["pages"] for op in page["operations"]]
        self.assertTrue(all(op["action"] == "delete" for op in operations))
        final = apply_chain(base, bundle, 5)
        self.assertEqual(final["awards"], [])
        # The retained directory row is untouched.
        self.assertEqual(final["suppliers"], [supplier("V1", "R1")])

    def test_supplier_delete_with_independent_award_deletion(self):
        # V1 disappears together with its award; an unrelated award on V2 is
        # withdrawn at the same time.  Both deletions must be plannable.
        base = make_mirror(
            "T",
            0,
            [supplier("V1", "R1"), supplier("V2", "R2")],
            [award("P1", "L1", "O1", "V1", 10), award("P2", "L1", "O2", "V2", 20)],
        )
        release = make_release(
            "T",
            [supplier("V2", "R2")],
            [("P2", [unawarded_lot("L1", vendor_id="V2")])],
        )
        bundle = build_bundle(base, release, 1)
        final = apply_chain(base, bundle, 1)
        self.assertEqual(final["suppliers"], [supplier("V2", "R2")])
        self.assertEqual(final["awards"], [])

    def test_supplier_insert_with_independent_award_deletion(self):
        base = make_mirror(
            "T",
            0,
            [supplier("V1", "R1")],
            [award("P1", "L1", "O1", "V1", 10)],
        )
        release = make_release(
            "T",
            [supplier("V1", "R1"), supplier("V2", "R2")],
            [("P1", [unawarded_lot("L1", vendor_id="V1")])],
        )
        bundle = build_bundle(base, release, 1)
        final = apply_chain(base, bundle, 1)
        self.assertEqual(
            final["suppliers"],
            [supplier("V1", "R1"), supplier("V2", "R2")],
        )
        self.assertEqual(final["awards"], [])

    def test_each_changed_key_appears_once_with_final_row_only(self):
        base = handover_base()
        bundle = build_bundle(base, handover_release(), 10)
        seen = []
        for page in bundle["pages"]:
            for op in page["operations"]:
                seen.append((op["table"], json.dumps(op["key"], sort_keys=True)))
        self.assertEqual(len(seen), len(set(seen)))
        # No temporary rows: every upsert already carries the target row.
        target_suppliers, target_awards = target_rows(handover_release())
        upserted_suppliers = {
            op["row"]["vendor_id"]: op["row"]
            for page in bundle["pages"]
            for op in page["operations"]
            if op["table"] == "suppliers" and op["action"] == "upsert"
        }
        for row in target_suppliers:
            if row["vendor_id"] in upserted_suppliers:
                self.assertEqual(upserted_suppliers[row["vendor_id"]], row)

    def test_deterministic_under_input_reordering(self):
        import random

        base = handover_base()
        release = handover_release()
        expected = json.dumps(build_bundle(base, release, 3 + 1), sort_keys=True)
        rng = random.Random(20260305)

        def reorder(value):
            if isinstance(value, dict):
                items = list(value.items())
                rng.shuffle(items)
                return {key: reorder(item) for key, item in items}
            if isinstance(value, list):
                items = [reorder(item) for item in value]
                rng.shuffle(items)
                return items
            return value

        for _ in range(5):
            shuffled = json.dumps(
                build_bundle(
                    reorder(copy.deepcopy(base)),
                    reorder(copy.deepcopy(release)),
                    4,
                ),
                sort_keys=True,
            )
            self.assertEqual(shuffled, expected)


class ReceptionTests(unittest.TestCase):
    def setUp(self):
        self.base = handover_base()
        self.bundle = build_bundle(self.base, handover_release(), 4)

    def test_successful_multi_page_continuation(self):
        current = self.base
        positions = [0]
        for page in self.bundle["pages"]:
            current = apply_page(current, page, 4)
            positions.append(current["position"])
        self.assertEqual(positions, [0, 1, 2])
        suppliers, awards = target_rows(handover_release())
        self.assertEqual(current["suppliers"], suppliers)
        self.assertEqual(current["awards"], awards)

    def test_same_position_different_baseline_rejected(self):
        other = copy.deepcopy(self.base)
        other["suppliers"][0]["public_ref"] = "DIFFERENT"
        with self.assertRaises(PublishError) as caught:
            apply_page(other, self.bundle["pages"][0], 4)
        self.assertIn("before_sha256", str(caught.exception))

    def test_skip_page_rejected_by_position(self):
        with self.assertRaises(PublishError) as caught:
            apply_page(self.base, self.bundle["pages"][1], 4)
        self.assertIn("position", str(caught.exception))

    def test_old_page_rejected_after_advancing(self):
        first = apply_page(self.base, self.bundle["pages"][0], 4)
        with self.assertRaises(PublishError):
            apply_page(first, self.bundle["pages"][0], 4)

    def test_duplicate_import_rejected(self):
        first = apply_page(self.base, self.bundle["pages"][0], 4)
        with self.assertRaises(PublishError):
            apply_page(first, self.bundle["pages"][0], 4)

    def test_stream_mismatch_rejected(self):
        page = copy.deepcopy(self.bundle["pages"][0])
        page["stream_id"] = "OTHER-STREAM"
        with self.assertRaises(PublishError) as caught:
            apply_page(self.base, page, 4)
        self.assertIn("stream_id", str(caught.exception))

    def test_limit_enforced(self):
        # Build a one-page bundle with several operations.
        release = handover_release()
        big = build_bundle(self.base, release, 10)
        self.assertGreaterEqual(len(big["pages"][0]["operations"]), 4)
        with self.assertRaises(PublishError) as caught:
            apply_page(self.base, big["pages"][0], 3)
        self.assertIn("max-ops", str(caught.exception))

    def test_empty_page_rejected(self):
        page = copy.deepcopy(self.bundle["pages"][0])
        page["operations"] = []
        with self.assertRaises(PublishError):
            apply_page(self.base, page, 4)

    def test_corrupt_operation_rejected(self):
        page = copy.deepcopy(self.bundle["pages"][0])
        for op in page["operations"]:
            if op["action"] == "upsert":
                op["row"] = {"unexpected": "shape"}
                break
        with self.assertRaises(PublishError):
            apply_page(self.base, page, 4)

    def test_corrupt_digest_rejected(self):
        page = copy.deepcopy(self.bundle["pages"][0])
        page["after_sha256"] = "0" * 64
        with self.assertRaises(PublishError) as caught:
            apply_page(self.base, page, 4)
        self.assertIn("after_sha256", str(caught.exception))

    def test_duplicate_operation_key_within_page_rejected(self):
        page = copy.deepcopy(self.bundle["pages"][0])
        page["operations"].append(copy.deepcopy(page["operations"][0]))
        with self.assertRaises(PublishError) as caught:
            apply_page(self.base, page, 5)
        self.assertIn("repeats operation key", str(caught.exception))

    def test_delete_missing_row_rejected(self):
        page = {
            "schema_version": 1,
            "stream_id": "STREAM-1",
            "from_position": 0,
            "before_sha256": mirror_sha256(validate_mirror(self.base)),
            "after_sha256": "0" * 64,
            "operations": [
                {
                    "table": "suppliers",
                    "action": "delete",
                    "key": "NOBODY",
                }
            ],
        }
        with self.assertRaises(PublishError) as caught:
            apply_page(self.base, page, 4)
        self.assertIn("does not exist", str(caught.exception))

    def test_unchanged_upsert_rejected(self):
        page = {
            "schema_version": 1,
            "stream_id": "STREAM-1",
            "from_position": 0,
            "before_sha256": mirror_sha256(validate_mirror(self.base)),
            "after_sha256": "0" * 64,
            "operations": [
                {
                    "table": "suppliers",
                    "action": "upsert",
                    "key": "S1",
                    "row": supplier("S1", "R1"),
                }
            ],
        }
        with self.assertRaises(PublishError) as caught:
            apply_page(self.base, page, 4)
        self.assertIn("unchanged row", str(caught.exception))

    def test_row_key_mismatch_rejected(self):
        page = copy.deepcopy(self.bundle["pages"][0])
        for op in page["operations"]:
            if op["table"] == "suppliers" and "row" in op:
                op["row"]["vendor_id"] = "WRONG"
                break
        with self.assertRaises(PublishError):
            apply_page(self.base, page, 4)

    def test_poisoned_page_with_matching_digest_but_broken_reference(self):
        """The receiver independently rejects an internally consistent page
        whose claimed result violates the known-vendor invariant, even
        though both hashes honestly describe the before/after bytes."""
        state = validate_mirror(self.base)
        evil = {
            "table": "awards",
            "action": "upsert",
            "key": ["P9", "L9"],
            "row": award("P9", "L9", "O9", "GHOST", 1),
        }
        candidate = _apply_operations(
            state, [_validate_operation(evil, "evil")]
        )
        poisoned = {
            "schema_version": 1,
            "stream_id": "STREAM-1",
            "from_position": 0,
            "before_sha256": mirror_sha256(state),
            "after_sha256": mirror_sha256(candidate),
            "operations": [evil],
        }
        # The after digest really does describe the invalid candidate.
        self.assertEqual(
            poisoned["after_sha256"], mirror_sha256(candidate)
        )
        with self.assertRaises(PublishError) as caught:
            apply_page(self.base, poisoned, 4)
        self.assertIn("unknown supplier", str(caught.exception))

    def test_poisoned_page_with_matching_digest_but_duplicate_ref(self):
        state = validate_mirror(self.base)
        evil = {
            "table": "suppliers",
            "action": "upsert",
            "key": "S4",
            "row": supplier("S4", "R1"),  # R1 is already held by S1
        }
        candidate = _apply_operations(
            state, [_validate_operation(evil, "evil")]
        )
        poisoned = {
            "schema_version": 1,
            "stream_id": "STREAM-1",
            "from_position": 0,
            "before_sha256": mirror_sha256(state),
            "after_sha256": mirror_sha256(candidate),
            "operations": [evil],
        }
        with self.assertRaises(PublishError) as caught:
            apply_page(self.base, poisoned, 4)
        self.assertIn("public_ref", str(caught.exception))

    def test_unknown_table_action_and_bad_key_shapes_rejected(self):
        state = validate_mirror(self.base)
        good_digest = mirror_sha256(state)
        for operation in (
            {"table": "payments", "action": "upsert", "key": "S1",
             "row": supplier("S1", "R9")},
            {"table": "suppliers", "action": "merge", "key": "S1"},
            {"table": "awards", "action": "delete", "key": ["P1"]},
            {"table": "suppliers", "action": "delete", "key": ""},
        ):
            page = {
                "schema_version": 1,
                "stream_id": "STREAM-1",
                "from_position": 0,
                "before_sha256": good_digest,
                "after_sha256": "0" * 64,
                "operations": [operation],
            }
            with self.assertRaises(PublishError):
                apply_page(self.base, page, 4)

    def test_delete_with_row_and_upsert_without_row_rejected(self):
        state = validate_mirror(self.base)
        good_digest = mirror_sha256(state)
        pages = [
            {
                "schema_version": 1,
                "stream_id": "STREAM-1",
                "from_position": 0,
                "before_sha256": good_digest,
                "after_sha256": "0" * 64,
                "operations": [
                    {
                        "table": "suppliers",
                        "action": "delete",
                        "key": "S1",
                        "row": supplier("S1", "R1"),
                    }
                ],
            },
            {
                "schema_version": 1,
                "stream_id": "STREAM-1",
                "from_position": 0,
                "before_sha256": good_digest,
                "after_sha256": "0" * 64,
                "operations": [
                    {"table": "suppliers", "action": "upsert", "key": "S9"}
                ],
            },
        ]
        for page in pages:
            with self.assertRaises(PublishError):
                apply_page(self.base, page, 4)

    def test_malformed_digest_shape_rejected(self):
        page = copy.deepcopy(self.bundle["pages"][0])
        page["before_sha256"] = "not-a-digest"
        with self.assertRaises(PublishError):
            apply_page(self.base, page, 4)

    def test_operation_extra_field_rejected(self):
        page = copy.deepcopy(self.bundle["pages"][0])
        page["operations"][0]["surprise"] = True
        with self.assertRaises(PublishError):
            apply_page(self.base, page, 5)


class BidReviewIntegrationTests(unittest.TestCase):
    def _review_package(self, package_id):
        data = {
            "package_budget_cents": 100000,
            "vendors": [
                {"id": "V1", "concurrent_lot_capacity": 2},
                {"id": "V2", "concurrent_lot_capacity": 2},
            ],
            "lots": [
                {
                    "id": "L1",
                    "deadline": "2026-03-10T17:00:00+00:00",
                    "budget_cents": 60000,
                    "required_documents": ["doc"],
                    "service_start": "2026-04-01",
                    "service_end": "2026-05-01",
                },
                {
                    "id": "L2",
                    "deadline": "2026-03-10T17:00:00+00:00",
                    "budget_cents": 60000,
                    "required_documents": ["doc"],
                    "service_start": "2026-05-01",
                    "service_end": "2026-06-01",
                },
            ],
            "offers": [
                {
                    "id": "O1",
                    "lot_id": "L1",
                    "vendor_id": "V1",
                    "submitted": "2026-03-01T10:00:00+00:00",
                    "price_cents": 30000,
                    "documents": ["doc"],
                },
                {
                    "id": "O2",
                    "lot_id": "L2",
                    "vendor_id": "V2",
                    "submitted": "2026-03-01T10:00:00+00:00",
                    "price_cents": 25000,
                    "documents": ["doc"],
                },
            ],
        }
        return package_id, review(data)

    def test_real_review_result_projects_and_publishes(self):
        package_id, result = self._review_package("PKG-REVIEW")
        release = {
            "schema_version": 1,
            "stream_id": "R",
            "suppliers": [
                supplier("V1", "PUB-1"),
                supplier("V2", "PUB-2"),
            ],
            "packages": [{"package_id": package_id, "result": result}],
        }
        base = make_mirror("R", 0, [supplier("V1", "PUB-1")], [])
        bundle = build_bundle(base, release, 2)
        final = apply_chain(base, bundle, 2)
        self.assertEqual(
            final["awards"],
            [
                award("PKG-REVIEW", "L1", "O1", "V1", 30000),
                award("PKG-REVIEW", "L2", "O2", "V2", 25000),
            ],
        )


class HashAndCanonicalTests(unittest.TestCase):
    def test_canonical_serialisation_matches_spec(self):
        mirror = make_mirror(
            "流",
            7,
            [supplier("S2", "公开2"), supplier("S1", "公开1")],
            [award("P2", "L1", "O2", "S2", 2), award("P1", "L1", "O1", "S1", 1)],
        )
        raw = canonical_mirror_bytes(validate_mirror(mirror)).decode("utf-8")
        rebuilt = json.loads(raw)
        # Rows are emitted in stable-key order, no whitespace or trailing
        # newline, non-ASCII is not escaped.
        self.assertEqual(
            [row["vendor_id"] for row in rebuilt["suppliers"]], ["S1", "S2"]
        )
        self.assertEqual(
            [(row["package_id"], row["lot_id"]) for row in rebuilt["awards"]],
            [("P1", "L1"), ("P2", "L1")],
        )
        self.assertNotIn("\\u", raw)
        self.assertNotIn(" ", raw)
        self.assertFalse(raw.endswith("\n"))
        expected = json.dumps(
            rebuilt, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(raw.encode("utf-8"), expected)
        self.assertEqual(mirror_sha256(validate_mirror(mirror)),
                         hashlib.sha256(expected).hexdigest())

    def test_position_participates_in_digest(self):
        first = validate_mirror(handover_base())
        second = copy.deepcopy(first)
        second["position"] = 1
        self.assertNotEqual(mirror_sha256(first), mirror_sha256(second))


class ValidationTests(unittest.TestCase):
    def test_release_award_field_consistency_checks(self):
        good = handover_release()
        validate_release(good)

        bad = copy.deepcopy(good)
        lot = bad["packages"][0]["result"]["lots"][0]
        lot["award_vendor_id"] = "SOMEONE-ELSE"
        with self.assertRaises(PublishError):
            validate_release(bad)

        bad = copy.deepcopy(good)
        lot = bad["packages"][0]["result"]["lots"][0]
        lot["offers"][0]["awarded"] = False
        with self.assertRaises(PublishError):
            validate_release(bad)

        bad = copy.deepcopy(good)
        lot = bad["packages"][0]["result"]["lots"][0]
        lot["award_offer_id"] = None
        with self.assertRaises(PublishError):
            validate_release(bad)

    def test_unawarded_lot_must_have_no_awarded_offer(self):
        release = make_release(
            "T",
            [supplier("V1", "R1")],
            [("P", [unawarded_lot("L1")])],
        )
        validate_release(release)
        release["packages"][0]["result"]["lots"][0]["offers"][0]["awarded"] = True
        with self.assertRaises(PublishError):
            validate_release(release)

    def test_duplicate_rows_and_refs_rejected(self):
        bad = make_mirror(
            "T", 0,
            [supplier("V1", "R1"), supplier("V2", "R1")],
            [],
        )
        with self.assertRaises(PublishError) as caught:
            validate_mirror(bad)
        self.assertIn("public_ref", str(caught.exception))

        bad = make_mirror(
            "T", 0,
            [supplier("V1", "R1"), supplier("V1", "R2")],
            [],
        )
        with self.assertRaises(PublishError):
            validate_mirror(bad)

        bad = make_mirror(
            "T", 0,
            [supplier("V1", "R1")],
            [award("P", "L1", "O1", "V1", 1), award("P", "L1", "O2", "V1", 2)],
        )
        with self.assertRaises(PublishError):
            validate_mirror(bad)

        bad = make_mirror(
            "T", 0,
            [supplier("V1", "R1")],
            [award("P", "L1", "O1", "GHOST", 1)],
        )
        with self.assertRaises(PublishError):
            validate_mirror(bad)

    def test_duplicate_packages_lots_offers_rejected(self):
        release = handover_release()
        bad = copy.deepcopy(release)
        bad["packages"].append(copy.deepcopy(bad["packages"][0]))
        with self.assertRaises(PublishError):
            validate_release(bad)

        bad = copy.deepcopy(release)
        lots = bad["packages"][0]["result"]["lots"]
        lots.append(copy.deepcopy(lots[0]))
        with self.assertRaises(PublishError):
            validate_release(bad)

        bad = copy.deepcopy(release)
        offers = bad["packages"][0]["result"]["lots"][0]["offers"]
        offers.append(copy.deepcopy(offers[0]))
        with self.assertRaises(PublishError):
            validate_release(bad)

    def test_duplicate_unawarded_lot_ids_rejected(self):
        release = make_release(
            "T",
            [supplier("V1", "R1")],
            [
                (
                    "P",
                    [
                        unawarded_lot("LDUP", vendor_id="V1"),
                        unawarded_lot("LDUP", vendor_id="V1"),
                    ],
                )
            ],
        )
        with self.assertRaises(PublishError) as caught:
            validate_release(release)
        self.assertIn("repeats lot_id", str(caught.exception))

    def test_empty_offers_rejected(self):
        release = handover_release()
        release["packages"][0]["result"]["lots"][0]["offers"] = []
        with self.assertRaises(PublishError):
            validate_release(release)

    def test_fractional_numbers_rejected_everywhere(self):
        bad = handover_base()
        bad["awards"][0]["price_cents"] = 1.5
        with self.assertRaises(PublishError):
            validate_mirror(bad)
        bad = handover_release()
        bad["suppliers"][0] = ["not", "an", "object"]
        with self.assertRaises(PublishError):
            validate_release(bad)

    def test_wrong_schema_and_shapes_rejected(self):
        bad = handover_base()
        bad["schema_version"] = 2
        with self.assertRaises(PublishError):
            validate_mirror(bad)
        bad = handover_base()
        bad["position"] = -1
        with self.assertRaises(PublishError):
            validate_mirror(bad)
        with self.assertRaises(PublishError):
            validate_mirror(["not", "an", "object"])
        with self.assertRaises(PublishError):
            validate_release("nope")

    def test_stream_mismatch_between_mirror_and_release(self):
        release = handover_release()
        release["stream_id"] = "OTHER"
        with self.assertRaises(PublishError):
            build_bundle(handover_base(), release, 4)

    def test_booleans_rejected_for_integers(self):
        for field, value in (
            ("schema_version", True),
            ("position", True),
        ):
            bad = handover_base()
            bad[field] = value
            with self.assertRaises(PublishError):
                validate_mirror(bad)
        bad = handover_release()
        bad["packages"][0]["result"]["lots"][0]["offers"][0]["price_cents"] = True
        with self.assertRaises(PublishError):
            validate_release(bad)

    def test_extra_fields_rejected_except_review_projections(self):
        bad = handover_base()
        bad["suppliers"][0]["unexpected"] = 1
        with self.assertRaises(PublishError):
            validate_mirror(bad)
        # Extra fields inside the bid-review result, lot, and offer objects
        # are tolerated because they are existing public projections.
        good = handover_release()
        result = good["packages"][0]["result"]
        result["awarded"] = 1
        result["lots"][0]["service_start"] = "2026-04-01"
        result["lots"][0]["offers"][0]["eligible"] = True
        validate_release(good)

    def test_strict_json_rejects_duplicate_keys_and_non_finite(self):
        with self.assertRaises(PublishError):
            parse_json_text('{"a": 1, "a": 2}', "dup")
        for text in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'):
            with self.assertRaises(PublishError):
                parse_json_text(text, "nonfinite")
        with self.assertRaises(PublishError):
            parse_json_text("{not json", "broken")

    def test_max_ops_must_be_positive_integer(self):
        for value in (0, -1, True):
            with self.assertRaises(PublishError):
                build_bundle(handover_base(), handover_release(), value)
            with self.assertRaises(PublishError):
                apply_page(
                    handover_base(),
                    build_bundle(handover_base(), handover_release(), 4)["pages"][0],
                    value,
                )


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "procurement.publish_cli", *map(str, args)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_build_and_apply_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            base_path = tmp / "base.json"
            release_path = tmp / "release.json"
            bundle_path = tmp / "bundle.json"
            base_path.write_text(
                json.dumps(handover_base()), encoding="utf-8"
            )
            release_path.write_text(
                json.dumps(handover_release()), encoding="utf-8"
            )
            result = self.run_cli(
                "build",
                "--base", base_path,
                "--release", release_path,
                "--max-ops", 4,
                "--output", bundle_path,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            self.assertEqual(len(bundle["pages"]), 2)

            current = base_path
            for index, page in enumerate(bundle["pages"]):
                page_path = tmp / f"page-{index}.json"
                next_path = tmp / f"next-{index}.json"
                page_path.write_text(json.dumps(page), encoding="utf-8")
                result = self.run_cli(
                    "apply",
                    "--base", current,
                    "--page", page_path,
                    "--max-ops", 4,
                    "--output", next_path,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                current = next_path
            final = json.loads(current.read_text(encoding="utf-8"))
            suppliers, awards = target_rows(handover_release())
            self.assertEqual(final["suppliers"], suppliers)
            self.assertEqual(final["awards"], awards)
            self.assertEqual(final["position"], 2)

    def test_failure_does_not_overwrite_output_or_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            base_path = tmp / "base.json"
            release_path = tmp / "release.json"
            out_path = tmp / "out.json"
            base_path.write_text(json.dumps(handover_base()), encoding="utf-8")
            release_path.write_text(
                json.dumps(handover_release()), encoding="utf-8"
            )
            out_path.write_text("KEEP-ME", encoding="utf-8")
            result = self.run_cli(
                "build",
                "--base", base_path,
                "--release", release_path,
                "--max-ops", 3,
                "--output", out_path,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(out_path.read_text(encoding="utf-8"), "KEEP-ME")

            # Same guarantee for apply: existing output is untouched.
            bundle = build_bundle(handover_base(), handover_release(), 4)
            page_path = tmp / "page.json"
            page_path.write_text(
                json.dumps(bundle["pages"][0]), encoding="utf-8"
            )
            result = self.run_cli(
                "apply",
                "--base", base_path,
                "--page", page_path,
                "--max-ops", 4,
                "--output", out_path,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(out_path.read_text(encoding="utf-8"), "KEEP-ME")

    def test_invalid_json_file_fails_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bad = tmp / "bad.json"
            bad.write_text("{broken", encoding="utf-8")
            result = self.run_cli(
                "build",
                "--base", bad,
                "--release", bad,
                "--max-ops", 4,
                "--output", tmp / "out.json",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid JSON", result.stderr)

    def test_committed_fixtures_replay_through_cli(self):
        fixture_dir = ROOT / "fixtures" / "publishing"
        bundle = json.loads(
            (fixture_dir / "bundle.json").read_text(encoding="utf-8")
        )
        expected = json.loads(
            (fixture_dir / "expected-next-mirror.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            current = tmp / "mirror.json"
            current.write_text(
                (fixture_dir / "base-mirror.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            for index, page in enumerate(bundle["pages"]):
                page_path = tmp / f"page-{index}.json"
                page_path.write_text(json.dumps(page), encoding="utf-8")
                next_path = tmp / f"mirror-{index + 1}.json"
                result = self.run_cli(
                    "apply",
                    "--base", current,
                    "--page", page_path,
                    "--max-ops", 4,
                    "--output", next_path,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                current = next_path
            self.assertEqual(
                json.loads(current.read_text(encoding="utf-8")), expected
            )


if __name__ == "__main__":
    unittest.main()
