import argparse
import json

from asset_schedule.io import load, publish

from .service import run


def main():
    parser = argparse.ArgumentParser(description="待排检修与连续用机的联合调度")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run(load(args.source))
        publish(args.output, result)
        brief = {"status": result["status"]}
        if "score" in result:
            brief.update(result["score"])
        print(json.dumps(brief, ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"asset-dispatch: {exc}\n")


if __name__ == "__main__":
    main()
