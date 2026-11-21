import argparse
import json
from asset_schedule.io import load, publish
from .service import run


def main():
    parser = argparse.ArgumentParser(description="固定资产关账流水与可对账月报")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run(load(args.source))
        publish(args.output, result)
        print(json.dumps({"first_close_month": result["first_close_month"],
                          "as_of_month": result["as_of_month"],
                          "journal_rows": len(result["journal"]),
                          "periods": len(result["periods"])}, ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"asset-close: {exc}\n")


if __name__ == "__main__":
    main()
