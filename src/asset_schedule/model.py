from datetime import date
from .io import fields, integer, records


def month(value):
    if not isinstance(value, str) or len(value) != 7:
        raise ValueError("month must be canonical YYYY-MM")
    try:
        day = date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise ValueError("invalid month") from exc
    if day.strftime("%Y-%m") != value:
        raise ValueError("month must be canonical YYYY-MM")
    return day.year * 12 + day.month - 1


def name(index):
    year, zero_month = divmod(index, 12)
    return f"{year:04d}-{zero_month + 1:02d}"


def validate(data):
    fields(data, ("as_of_month", "assets"))
    cutoff = month(data["as_of_month"])
    assets = records(data["assets"], "asset", 1, 200)
    for asset in assets.values():
        fields(asset, ("id", "category", "cost_cents", "salvage_cents", "life_months",
                       "in_service_month", "disposal"))
        if not isinstance(asset["category"], str) or not asset["category"].strip() or len(asset["category"]) > 100:
            raise ValueError("category requires nonempty string <=100")
        cost = integer(asset["cost_cents"], "cost_cents", 0, 10**12)
        salvage = integer(asset["salvage_cents"], "salvage_cents", 0, 10**12)
        if salvage > cost:
            raise ValueError("salvage exceeds cost")
        integer(asset["life_months"], "life_months", 1, 600)
        start = month(asset["in_service_month"])
        if start + asset["life_months"] - 1 > 9999 * 12 + 11:
            raise ValueError("service life exceeds supported calendar")
        disposal = asset["disposal"]
        if disposal is not None:
            fields(disposal, ("month", "proceeds_cents"))
            if month(disposal["month"]) < start:
                raise ValueError("disposal precedes service")
            integer(disposal["proceeds_cents"], "proceeds_cents", 0, 10**12)
    return cutoff, assets
