"""Command line entry point for paged mirror publication and reception."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .publishing import (
    PublishError,
    apply_page,
    build_bundle,
    load_json_file,
    validate_bundle,
)


def _write_new_file(path, payload):
    """Write ``payload`` only when the target does not already exist."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".bid-publish-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, target)
        except FileExistsError as exc:
            raise PublishError(
                f"output {target} already exists; refusing to overwrite it"
            ) from exc
        except OSError:
            # Filesystem without hard links; the complete temporary file can
            # be renamed after a final existence check.
            if target.exists():
                raise PublishError(
                    f"output {target} already exists; refusing to overwrite it"
                )
            os.replace(tmp_name, target)
            tmp_name = None
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _build(args):
    base = load_json_file(args.base)
    release = load_json_file(args.release)
    bundle = build_bundle(base, release, args.max_ops)
    _write_new_file(args.output, bundle)
    print(
        json.dumps(
            {
                "stream_id": bundle["stream_id"],
                "pages": len(bundle["pages"]),
                "output": args.output,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )


def _apply(args):
    base = load_json_file(args.base)
    page = load_json_file(args.page)
    next_mirror = apply_page(base, page, args.max_ops)
    _write_new_file(args.output, next_mirror)
    print(
        json.dumps(
            {
                "stream_id": next_mirror["stream_id"],
                "position": next_mirror["position"],
                "suppliers": len(next_mirror["suppliers"]),
                "awards": len(next_mirror["awards"]),
                "output": args.output,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bid-publish")
    sub = parser.add_subparsers(dest="command", required=True)

    build_parser = sub.add_parser(
        "build",
        help="build a paged change bundle from a base mirror and a release",
    )
    build_parser.add_argument("--base", required=True)
    build_parser.add_argument("--release", required=True)
    build_parser.add_argument("--max-ops", required=True, type=int)
    build_parser.add_argument("--output", required=True)
    build_parser.set_defaults(handler=_build)

    apply_parser = sub.add_parser(
        "apply",
        help="validate and apply one page to a mirror",
    )
    apply_parser.add_argument("--base", required=True)
    apply_parser.add_argument("--page", required=True)
    apply_parser.add_argument("--max-ops", required=True, type=int)
    apply_parser.add_argument("--output", required=True)
    apply_parser.set_defaults(handler=_apply)

    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except PublishError as exc:
        print(f"bid-publish: {exc}", file=sys.stderr)
        return 2
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"bid-publish: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
