from .model import validate
from .depreciation import schedule


def run(data):
    cutoff, assets = validate(data)
    results = [schedule(asset, cutoff) for _, asset in sorted(assets.items())]
    categories = {}
    for result in results:
        total = categories.setdefault(result["category"], {"closing_book_cents": 0, "expense_cents": 0,
                                                            "disposal_gain_loss_cents": 0})
        total["closing_book_cents"] += result["closing_book_cents"]
        total["expense_cents"] += result["accumulated_cents"]
        if result["disposal_result"]:
            total["disposal_gain_loss_cents"] += result["disposal_result"]["gain_loss_cents"]
    return {"schema_version": 1, "as_of_month": data["as_of_month"], "assets": results,
            "categories": dict(sorted(categories.items())),
            "summary": {"assets": len(results), "disposed": sum(r["status"] == "disposed" for r in results),
                        "closing_book_cents": sum(r["closing_book_cents"] for r in results),
                        "cumulative_depreciation_cents": sum(r["accumulated_cents"] for r in results),
                        "disposal_gain_loss_cents": sum(r["disposal_result"]["gain_loss_cents"]
                                                        for r in results if r["disposal_result"])}}
