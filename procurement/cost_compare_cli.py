"""Command line entry point for the bid-cost-compare analysis."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    # Allow `python -I procurement/cost_compare_cli.py` for isolated local
    # runs; the normal installed console script uses the package import below.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from procurement.costcompare import (  # type: ignore[no-redef]
        CostCompareError,
        compare_bid_costs,
        load_json_file,
    )
else:
    from .costcompare import CostCompareError, compare_bid_costs, load_json_file


def _write_new_file(path, payload):
    """Write ``payload`` only when the target does not already exist."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".bid-cost-compare-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, target)
        except FileExistsError as exc:
            raise CostCompareError(
                f"output {target} already exists; refusing to overwrite it"
            ) from exc
        except OSError:
            # Filesystem without hard links; the complete temporary file can
            # be renamed after a final existence check.
            if target.exists():
                raise CostCompareError(
                    f"output {target} already exists; refusing to overwrite it"
                )
            os.replace(tmp_name, target)
            tmp_name = None
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bid-cost-compare")
    parser.add_argument("--review", required=True)
    parser.add_argument("--annex", required=True)
    parser.add_argument("--basket", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        review = load_json_file(args.review)
        annex = load_json_file(args.annex)
        basket = load_json_file(args.basket)
        result = compare_bid_costs(review, annex, basket)
        # Serialise before touching the filesystem so an output-shape error
        # can never create a partial result file.
        json.dumps(result, indent=2, ensure_ascii=False)
        _write_new_file(args.output, result)
        participants = [
            row["offer_id"]
            for row in result["offers"]
            if row["participates_in_comparison"]
        ]
        print(
            json.dumps(
                {
                    "lot_id": result["lot_id"],
                    "offers_checked": len(result["offers"]),
                    "offers_compared": participants,
                    "pairwise_verdicts": len(result["pairwise_verdicts"]),
                    "output": args.output,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    except (OSError, CostCompareError, KeyError, TypeError, ValueError) as exc:
        print(f"bid-cost-compare: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
