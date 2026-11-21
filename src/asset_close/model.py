"""关账输入校验：沿用 v1 资产字段、单位与整数分约束，增加修订与时间窗规则。"""
from asset_schedule.io import fields, integer, label, rows
from asset_schedule.model import month

KINDS = ("cost", "depreciation", "carrying_removed", "proceeds")
MAX_RANGE_MONTHS = 120
MAX_REVISIONS = 2400
MAX_ASSETS = 200


def validate_asset(asset, first_close):
    """与 v1 相同的资产记录校验，另要求投入月份不早于首个关账月。"""
    if not isinstance(asset, dict):
        raise ValueError("asset must be an object")
    fields(asset, ("id", "category", "cost_cents", "salvage_cents", "life_months",
                   "in_service_month", "disposal"))
    label(asset["id"], "id")
    if not isinstance(asset["category"], str) or not asset["category"].strip() or len(asset["category"]) > 100:
        raise ValueError("category requires nonempty string <=100")
    cost = integer(asset["cost_cents"], "cost_cents", 0, 10**12)
    salvage = integer(asset["salvage_cents"], "salvage_cents", 0, 10**12)
    if salvage > cost:
        raise ValueError("salvage exceeds cost")
    life = integer(asset["life_months"], "life_months", 1, 600)
    start = month(asset["in_service_month"])
    if start < first_close:
        raise ValueError("in_service_month precedes first_close_month")
    if start + life - 1 > 9999 * 12 + 11:
        raise ValueError("service life exceeds supported calendar")
    disposal = asset["disposal"]
    if disposal is not None:
        if not isinstance(disposal, dict):
            raise ValueError("disposal must be null or an object")
        fields(disposal, ("month", "proceeds_cents"))
        if month(disposal["month"]) < start:
            raise ValueError("disposal precedes service")
        integer(disposal["proceeds_cents"], "proceeds_cents", 0, 10**12)
    return asset


def validate(data):
    """返回 (首关账月序号, 截止月序号, [(known_month 序号, asset), ...])，全部修订含未来版本均完整校验。"""
    fields(data, ("first_close_month", "as_of_month", "revisions"))
    first_close = month(data["first_close_month"])
    as_of = month(data["as_of_month"])
    span = as_of - first_close + 1
    if not 1 <= span <= MAX_RANGE_MONTHS:
        raise ValueError(f"close range must span 1..{MAX_RANGE_MONTHS} months")
    raw_revisions = rows(data["revisions"], "revisions", 0, MAX_REVISIONS)
    parsed = []
    seen = set()
    asset_ids = set()
    for revision in raw_revisions:
        if not isinstance(revision, dict):
            raise ValueError("revision must be an object")
        fields(revision, ("known_month", "asset"))
        known = month(revision["known_month"])
        if known < first_close:
            raise ValueError("known_month precedes first_close_month")
        asset = validate_asset(revision["asset"], first_close)
        pair = (known, asset["id"])
        if pair in seen:
            raise ValueError(f"duplicate revision for asset {asset['id']} in same known_month")
        seen.add(pair)
        asset_ids.add(asset["id"])
        parsed.append((known, asset))
    if len(asset_ids) > MAX_ASSETS:
        raise ValueError(f"at most {MAX_ASSETS} distinct asset IDs")
    return first_close, as_of, parsed
