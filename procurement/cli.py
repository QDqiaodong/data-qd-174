import argparse
import json
from pathlib import Path
from .core import review


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    try:
        result = review(json.loads(Path(a.input).read_text()))
        target = Path(a.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
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
