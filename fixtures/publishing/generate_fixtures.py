"""Regenerate the deterministic synthetic bid-publish fixtures.

Run from the repository root:

    python3 fixtures/publishing/generate_fixtures.py

The fixtures are deliberately tiny and fully synthetic.  The release file
embeds ``bid-review``-shaped package results (including extra projection
fields such as ``eligible`` and ``service_start`` that publication ignores)
so the fixture also proves that extra review fields are tolerated.

The script writes only the four fixture JSON files listed in SOURCES.md and
never reads anything outside this directory's intended inputs, so it is safe
to re-run; output content only depends on the constants below.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from procurement.publishing import (  # noqa: E402
    apply_page,
    build_bundle,
    mirror_sha256,
    validate_mirror,
)

HERE = Path(__file__).resolve().parent
STREAM = "demo-stream-2026"


def write_json(path, payload):
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def awarded_lot(lot_id, offer_id, vendor_id, price_cents, *, loser=None):
    offers = [
        {
            "offer_id": offer_id,
            "vendor_id": vendor_id,
            "price_cents": price_cents,
            "eligible": True,
            "reasons": [],
            "awarded": True,
            "selection_reasons": [],
        }
    ]
    if loser is not None:
        offers.append(
            {
                "offer_id": loser,
                "vendor_id": "V-GAMMA",
                "price_cents": price_cents + 500,
                "eligible": True,
                "reasons": [],
                "awarded": False,
                "selection_reasons": ["objective_tradeoff"],
            }
        )
    return {
        "lot_id": lot_id,
        "service_start": "2026-04-01",
        "service_end": "2026-05-01",
        "budget_cents": price_cents * 2,
        "offers": offers,
        "award_offer_id": offer_id,
        "award_vendor_id": vendor_id,
        "awarded": True,
    }


def unawarded_lot(lot_id, vendor_id):
    return {
        "lot_id": lot_id,
        "service_start": "2026-06-01",
        "service_end": "2026-07-01",
        "budget_cents": 9000,
        "offers": [
            {
                "offer_id": lot_id + "-O-LOST",
                "vendor_id": vendor_id,
                "price_cents": 8800,
                "eligible": False,
                "reasons": ["over_budget"],
                "awarded": False,
                "selection_reasons": [],
            }
        ],
        "award_offer_id": None,
        "award_vendor_id": None,
        "awarded": False,
    }


def main():
    # Base mirror held by an information station before this publication.
    base_mirror = {
        "schema_version": 1,
        "stream_id": STREAM,
        "position": 3,
        "suppliers": [
            {"vendor_id": "V-ALPHA", "public_ref": "PUB-100"},
            {"vendor_id": "V-BETA", "public_ref": "PUB-200"},
            {"vendor_id": "V-GAMMA", "public_ref": "PUB-300"},
        ],
        "awards": [
            {
                "package_id": "PKG-A",
                "lot_id": "LOT-1",
                "offer_id": "OFFER-A1",
                "vendor_id": "V-ALPHA",
                "price_cents": 42000,
            },
            {
                "package_id": "PKG-A",
                "lot_id": "LOT-2",
                "offer_id": "OFFER-A2",
                "vendor_id": "V-BETA",
                "price_cents": 17500,
            },
            {
                "package_id": "PKG-B",
                "lot_id": "LOT-1",
                "offer_id": "OFFER-B1",
                "vendor_id": "V-GAMMA",
                "price_cents": 9900,
            },
        ],
    }

    # New release from the procurement centre.  Changes:
    #   * V-ALPHA leaves the directory; V-DELTA joins and takes PUB-100;
    #   * the two PKG-A awards move onto V-DELTA (constrained handover);
    #   * PKG-A LOT-2 changes price;
    #   * PKG-B LOT-1 is withdrawn (lot becomes unawarded);
    #   * a new PKG-C LOT-7 is awarded to V-BETA.
    release = {
        "schema_version": 1,
        "stream_id": STREAM,
        "suppliers": [
            {"vendor_id": "V-BETA", "public_ref": "PUB-200"},
            {"vendor_id": "V-GAMMA", "public_ref": "PUB-300"},
            {"vendor_id": "V-DELTA", "public_ref": "PUB-100"},
        ],
        "packages": [
            {
                "package_id": "PKG-A",
                "result": {
                    "lots": [
                        awarded_lot(
                            "LOT-1", "OFFER-A1-NEW", "V-DELTA", 41000,
                            loser="OFFER-A1-RIVAL",
                        ),
                        awarded_lot("LOT-2", "OFFER-A2", "V-DELTA", 16900),
                    ],
                    "awarded": 2,
                },
            },
            {
                "package_id": "PKG-B",
                "result": {
                    "lots": [unawarded_lot("LOT-1", "V-GAMMA")],
                    "awarded": 0,
                },
            },
            {
                "package_id": "PKG-C",
                "result": {
                    "lots": [awarded_lot("LOT-7", "OFFER-C7", "V-BETA", 5300)],
                    "awarded": 1,
                },
            },
        ],
    }

    # max-ops 4 forces the constrained handover into one four-op page while
    # independent changes spread across further pages.
    bundle = build_bundle(base_mirror, release, max_ops=4)

    write_json(HERE / "base-mirror.json", base_mirror)
    write_json(HERE / "release.json", release)
    write_json(HERE / "bundle.json", bundle)

    # Materialise each page so a receiver can be fed one file at a time, and
    # precompute the expected final mirror by replaying every page.
    current = base_mirror
    for index, page in enumerate(bundle["pages"]):
        write_json(HERE / f"page-{index}.json", page)
        assert page["before_sha256"] == mirror_sha256(validate_mirror(current))
        current = apply_page(current, page, max_ops=4)
    write_json(HERE / "expected-next-mirror.json", current)

    print(
        json.dumps(
            {
                "stream_id": STREAM,
                "pages": len(bundle["pages"]),
                "page_sizes": [len(page["operations"]) for page in bundle["pages"]],
                "final_position": current["position"],
                "fixture_dir": str(HERE),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
