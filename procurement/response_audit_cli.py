"""Command line entry point for the response-time audit."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    # Allow `python -I procurement/response_audit_cli.py` for isolated local
    # runs; the normal installed console script uses the package import below.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from procurement.responseaudit import (  # type: ignore[no-redef]
        ResponseAuditError,
        audit_responses,
        load_json_file,
    )
else:
    from .responseaudit import (
        ResponseAuditError,
        audit_responses,
        load_json_file,
    )


def _write_new_file(path, payload):
    """Write ``payload`` only when the target does not already exist."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".bid-response-audit-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, target)
        except FileExistsError as exc:
            raise ResponseAuditError(
                f"output {target} already exists; refusing to overwrite it"
            ) from exc
        except OSError:
            # Filesystem without hard links; the complete temporary file can
            # be renamed after a final existence check.
            if target.exists():
                raise ResponseAuditError(
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
    parser = argparse.ArgumentParser(prog="bid-response-audit")
    parser.add_argument("--review", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        review = load_json_file(args.review)
        logs = load_json_file(args.input)
        result = audit_responses(review, logs)
        # Serialise before touching the filesystem so an output-shape error
        # can never create a partial result file.
        json.dumps(result, indent=2, ensure_ascii=False)
        _write_new_file(args.output, result)
        print(
            json.dumps(
                {
                    "lots": result["totals"]["lots"],
                    "sites": result["totals"]["sites"],
                    "tickets": result["totals"]["tickets"],
                    "max_pairs": result["totals"]["max_pairs"],
                    "output": args.output,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    except (OSError, ResponseAuditError, KeyError, TypeError, ValueError) as exc:
        print(f"bid-response-audit: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
