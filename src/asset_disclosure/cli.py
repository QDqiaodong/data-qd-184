import argparse
import json

from asset_schedule.io import load, publish

from .service import run


def main():
    parser = argparse.ArgumentParser(
        description="资产类别 × 保管单位期末余额的披露选择")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run(load(args.source))
        publish(args.output, result)
        print(json.dumps({"as_of_month": result["as_of_month"],
                          "status": result["status"]},
                         ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"asset-balance-disclosure: {exc}\n")


if __name__ == "__main__":
    main()
