"""asset-lineage 输入校验：沿用 v1 资产记录与整数分约束，增加改造/出售事件规则。"""
from asset_schedule.io import fields, integer, label, records, rows
from asset_schedule.model import month

KINDS = ("reconfigure", "sell")
MAX_RANGE_MONTHS = 120
MAX_ASSETS = 200
MAX_EVENTS = 1000
MAX_SIDE = 8
MAX_SEQ = 1_000_000
MAX_WEIGHT = 10_000


def validate_asset(asset, from_month, as_of_month):
    """与 v1 相同的资产记录校验，另要求投入月在报告区间内且 disposal 为 null。

    本命令的资产记录只代表原始投入；出售统一通过 sell 事件输入。
    """
    if not isinstance(asset, dict):
        raise ValueError("asset must be an object")
    fields(asset, ("id", "category", "cost_cents", "salvage_cents", "life_months",
                   "in_service_month", "disposal"))
    label(asset["id"], "id")
    if not isinstance(asset["category"], str) or not asset["category"].strip() \
            or len(asset["category"]) > 100:
        raise ValueError("category requires nonempty string <=100")
    cost = integer(asset["cost_cents"], "cost_cents", 0, 10**12)
    salvage = integer(asset["salvage_cents"], "salvage_cents", 0, 10**12)
    if salvage > cost:
        raise ValueError("salvage exceeds cost")
    life = integer(asset["life_months"], "life_months", 1, 600)
    start = month(asset["in_service_month"])
    if not from_month <= start <= as_of_month:
        raise ValueError("in_service_month outside report range")
    if start + life - 1 > 9999 * 12 + 11:
        raise ValueError("service life exceeds supported calendar")
    if asset["disposal"] is not None:
        raise ValueError("disposal must be null; disposals enter via sell events")
    return asset


def _category(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise ValueError("category requires nonempty string <=100")
    return value


def _reconfigure(raw):
    fields(raw, ("id", "month", "seq", "kind", "sources", "targets"))
    sources = []
    for value in rows(raw["sources"], "sources", 1, MAX_SIDE):
        source_id = label(value, "source")
        if source_id in sources:
            raise ValueError(f"duplicate source device ID: {source_id}")
        sources.append(source_id)
    targets = []
    for target in rows(raw["targets"], "targets", 1, MAX_SIDE):
        if not isinstance(target, dict):
            raise ValueError("target must be an object")
        fields(target, ("id", "category", "weight"))
        targets.append({"id": label(target["id"], "target.id"),
                        "category": _category(target["category"]),
                        "weight": integer(target["weight"], "weight", 1, MAX_WEIGHT)})
    if len(targets) == 1 and targets[0]["weight"] != 1:
        raise ValueError("single target requires weight 1")
    if len(sources) > 1 and len(targets) > 1:
        raise ValueError("many-to-many reconfigure is not supported")
    return {"sources": sources, "targets": targets}


def _sell(raw):
    fields(raw, ("id", "month", "seq", "kind", "asset_id", "proceeds_cents"))
    return {"asset_id": label(raw["asset_id"], "asset_id"),
            "proceeds_cents": integer(raw["proceeds_cents"], "proceeds_cents", 0, 10**12)}


def validate_events(value, asset_ids, from_month, as_of_month):
    """校验全部事件并返回解析列表；设备是否在册留待回放时判定。

    目标 ID 不得与任何原始资产或其他事件的目标 ID 相同，即使该 ID 还未生效
    或已经注销也不能使用；这一静态约束在此完整校验。
    """
    events = []
    seen_ids = set()
    seen_seq = set()
    target_ids = set()
    for raw in rows(value, "events", 0, MAX_EVENTS):
        if not isinstance(raw, dict):
            raise ValueError("event must be an object")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise ValueError(f"unknown event kind: {kind!r}")
        event_id = label(raw.get("id"), "event.id")
        if event_id in seen_ids:
            raise ValueError(f"duplicate event ID: {event_id}")
        seen_ids.add(event_id)
        event_month = month(raw.get("month"))
        if not from_month <= event_month <= as_of_month:
            raise ValueError("event month outside report range")
        seq = integer(raw.get("seq"), "seq", 1, MAX_SEQ)
        if (event_month, seq) in seen_seq:
            raise ValueError(f"duplicate seq {seq} in month of event {event_id}")
        seen_seq.add((event_month, seq))
        if kind == "reconfigure":
            body = _reconfigure(raw)
            for target in body["targets"]:
                target_id = target["id"]
                if target_id in asset_ids or target_id in target_ids:
                    raise ValueError(f"target ID reuses a device ID: {target_id}")
                target_ids.add(target_id)
        else:
            body = _sell(raw)
        events.append({"id": event_id, "month": event_month, "seq": seq,
                       "kind": kind, **body})
    return events


def validate(data):
    """返回 (起始月序号, 截止月序号, 原始投入 dict, 事件列表)。"""
    fields(data, ("from_month", "as_of_month", "assets", "events"))
    from_month = month(data["from_month"])
    as_of_month = month(data["as_of_month"])
    span = as_of_month - from_month + 1
    if not 1 <= span <= MAX_RANGE_MONTHS:
        raise ValueError(f"report range must span 1..{MAX_RANGE_MONTHS} months")
    assets = records(data["assets"], "asset", 0, MAX_ASSETS)
    for asset in assets.values():
        validate_asset(asset, from_month, as_of_month)
    events = validate_events(data["events"], set(assets), from_month, as_of_month)
    return from_month, as_of_month, assets, events
