import argparse
import json

from asset_schedule.io import load, publish

from .service import run


def main():
    parser = argparse.ArgumentParser(
        description="固定资产处置后的银行收款核销、退扣与催收交接")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run(load(args.source))
        publish(args.output, result)
        print(json.dumps({"through_month": result["through_month"],
                          "periods": len(result["periods"]),
                          "accounts": len(result["periods"][-1]["accounts"]),
                          "orders": sum(len(row["collection_orders"])
                                        for row in result["periods"])},
                         ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"asset-sale-collections: {exc}\n")


if __name__ == "__main__":
    main()
