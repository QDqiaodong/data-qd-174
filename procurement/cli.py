import argparse
import json
from pathlib import Path

from .core import review
from .qualification import QualificationError, load_json_file


def main(argv=None):
    p = argparse.ArgumentParser(prog="bid-review")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--qualification-file")
    p.add_argument("--qualification-as-of")
    a = p.parse_args(argv)
    try:
        package = load_json_file(a.input)
        qualification = None
        if a.qualification_file is not None and a.qualification_as_of is not None:
            qualification = load_json_file(a.qualification_file)
        elif a.qualification_file is not None or a.qualification_as_of is not None:
            raise QualificationError(
                "--qualification-file and --qualification-as-of must be used together"
            )
        result = review(package, qualification, a.qualification_as_of)
        # Serialise before touching the filesystem so an input or output-shape
        # error can never create a partial result file.
        payload = json.dumps(result, indent=2) + "\n"
        target = Path(a.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as f:
            f.write(payload)
        print(
            json.dumps(
                {
                    "lots": len(result["lots"]),
                    "awarded": result["awarded"],
                    "package_budget_cents": result["package"]["budget_cents"],
                    "awarded_total_cents": result["package"]["awarded_total_cents"],
                    "remaining_cents": result["package"]["remaining_cents"],
                },
                sort_keys=True,
            )
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"bid-review: {exc}\n")


if __name__ == "__main__":
    main()
