import argparse
import json

from asset_schedule.io import load, publish

from .service import run


def main():
    parser = argparse.ArgumentParser(description="资产拆并改造后的价值承继清单与按月台账")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run(load(args.source))
        publish(args.output, result)
        closing = sum(row["closing_book_cents"]
                      for row in result["periods"][-1]["assets"])
        print(json.dumps({"from_month": result["from_month"],
                          "as_of_month": result["as_of_month"],
                          "events": len(result["events"]),
                          "periods": len(result["periods"]),
                          "closing_book_cents": closing},
                         ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"asset-lineage: {exc}\n")


if __name__ == "__main__":
    main()
