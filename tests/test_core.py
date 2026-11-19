import json
import unittest
from pathlib import Path
from procurement.core import review


class ReviewTests(unittest.TestCase):
    def test_tie_breaks_by_vendor_and_excludes_late(self):
        result = review(json.loads(Path("fixtures/input.json").read_text()))
        self.assertEqual(result["lots"][0]["award_vendor_id"], "V1")
        self.assertEqual(result["lots"][1]["award_offer_id"], None)
        self.assertEqual(result["lots"][1]["offers"][0]["reasons"], ["late"])

    def test_rejects_unknown_lot(self):
        with self.assertRaisesRegex(ValueError, "unknown lot"):
            review({"lots": [], "offers": [{"id": "o", "lot_id": "x", "vendor_id": "v", "submitted": "2026-01-01T00:00:00+00:00", "price_cents": 1, "documents": []}]})


if __name__ == "__main__":
    unittest.main()
