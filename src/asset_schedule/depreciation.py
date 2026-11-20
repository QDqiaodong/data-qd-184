"""Declared whole-month straight-line convention with exact cumulative cent allocation."""
from .model import month, name


def schedule(asset, cutoff):
    start = month(asset["in_service_month"])
    life = asset["life_months"]
    base = asset["cost_cents"] - asset["salvage_cents"]
    disposal = asset["disposal"]
    dispose_month = month(disposal["month"]) if disposal else None
    # Disposal month is excluded from depreciation, even if it equals in-service month.
    stop = min(cutoff + 1, start + life, dispose_month if dispose_month is not None else cutoff + 1)
    months = max(0, stop - start)
    rows = []
    for i in range(1, months + 1):
        before = base * (i - 1) // life
        after = base * i // life
        rows.append({"month": name(start + i - 1), "opening_book_cents": asset["cost_cents"] - before,
                     "depreciation_cents": after - before, "accumulated_cents": after,
                     "closing_book_cents": asset["cost_cents"] - after})
    accumulated = base * months // life
    carrying = asset["cost_cents"] - accumulated
    disposed = dispose_month is not None and dispose_month <= cutoff
    if start > cutoff:
        status = "not_in_service"
    elif disposed:
        status = "disposed"
    elif months == life:
        status = "fully_depreciated"
    else:
        status = "in_service"
    disposal_result = None
    if disposed:
        disposal_result = {"month": disposal["month"], "carrying_cents": carrying,
                           "proceeds_cents": disposal["proceeds_cents"],
                           "gain_loss_cents": disposal["proceeds_cents"] - carrying}
    return {"asset_id": asset["id"], "category": asset["category"], "status": status,
            "cost_cents": asset["cost_cents"], "salvage_cents": asset["salvage_cents"],
            "depreciated_months": months, "accumulated_cents": accumulated,
            "carrying_before_disposal_cents": carrying if start <= cutoff else None,
            "closing_book_cents": carrying if start <= cutoff and not disposed else 0,
            "recognized_cost_cents": asset["cost_cents"] if start <= cutoff else 0,
            "monthly": rows, "disposal_result": disposal_result}
