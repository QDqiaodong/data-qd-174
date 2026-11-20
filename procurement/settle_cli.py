import argparse
import json
import sys

from .settlement import (
    ExportError,
    SettleError,
    apply_batch,
    initialize_ledger,
)


def _dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _init(args):
    state = initialize_ledger(args.input, args.state)
    print(
        _dump(
            {
                "state": args.state,
                "schema_version": state["schema_version"],
                "awarded_lots": sorted(state["lots"]),
                "awarded_total_cents": state["award"]["awarded_total_cents"],
                "contracts": [
                    {
                        "lot_id": contract["lot_id"],
                        "vendor_id": contract["vendor_id"],
                        "offer_id": contract["offer_id"],
                        "price_cents": contract["price_cents"],
                        "service_start": contract["service_start"],
                        "service_end": contract["service_end"],
                    }
                    for _, contract in sorted(state["contracts"].items())
                ],
            }
        )
    )


def _apply(args):
    report = apply_batch(args.state, args.batch, args.output)
    print(_dump(report))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bid-settle")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="freeze an award into a new ledger")
    init_parser.add_argument("--input", required=True)
    init_parser.add_argument("--state", required=True)
    init_parser.set_defaults(handler=_init)

    apply_parser = sub.add_parser("apply", help="apply one business batch")
    apply_parser.add_argument("--state", required=True)
    apply_parser.add_argument("--batch", required=True)
    apply_parser.add_argument("--output", required=True)
    apply_parser.set_defaults(handler=_apply)

    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except ExportError as exc:
        print(
            "bid-settle: ERROR: batch "
            f"{exc.batch_id!r} WAS ACCEPTED and committed; its report could not be "
            f"exported: {exc}. Re-run the identical batch to export it again "
            "without paying twice.",
            file=sys.stderr,
        )
        return 2
    except SettleError as exc:
        print(f"bid-settle: {exc}", file=sys.stderr)
        return 2
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"bid-settle: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
