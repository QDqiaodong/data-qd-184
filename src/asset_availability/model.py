"""Strict validation for the date-level availability report."""
import math
from datetime import date

from asset_schedule.io import fields

MAX_DAYS = 3660


def _reject_nonfinite(value):
    """Reject NaN/inf, including ``1e999`` which JSON encodes as a float."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("non-finite JSON number")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _list(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} requires an array")
    return value


def _date(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} requires canonical YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} requires canonical YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} requires canonical YYYY-MM-DD")
    return parsed


def _label(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} requires a nonempty string")
    return value


def _nonempty(start, end, name):
    if start >= end:
        raise ValueError(f"{name} interval must be nonempty")


def _bounded(asset, start, end, report_from, report_to, name):
    if start < asset["active_from"]:
        raise ValueError(f"{name} starts before the asset active interval")
    if asset["active_until"] is not None and end > asset["active_until"]:
        raise ValueError(f"{name} ends after the asset active interval")
    if start < report_from:
        raise ValueError(f"{name} starts before the report interval")
    if end > report_to:
        raise ValueError(f"{name} ends after the report interval")


def validate(data):
    """Return report bounds plus validated, date-parsed records.

    Every object is checked completely before any aggregation.  Record counts
    are not capped below the documented working capacity (500 assets, 10000
    orders, 20000 incidents); the repository's 8 MB source limit is the outer
    bound, so inputs above that capacity are still accepted when they fit.
    """
    _reject_nonfinite(data)
    fields(data, ("from_date", "to_date", "assets", "maintenance_orders", "incidents"))
    report_from = _date(data["from_date"], "from_date")
    report_to = _date(data["to_date"], "to_date")
    span = (report_to - report_from).days
    if not 1 <= span <= MAX_DAYS:
        raise ValueError(f"report interval must span 1..{MAX_DAYS} natural days")

    assets = {}
    for raw in _list(data["assets"], "assets"):
        fields(raw, ("id", "category", "active_from", "active_until"))
        asset_id = _label(raw["id"], "asset.id")
        if asset_id in assets:
            raise ValueError(f"duplicate asset ID: {asset_id}")
        category = _label(raw["category"], "asset.category")
        active_from = _date(raw["active_from"], "asset.active_from")
        active_until = (None if raw["active_until"] is None
                        else _date(raw["active_until"], "asset.active_until"))
        if active_until is not None:
            _nonempty(active_from, active_until, "asset active")
        window_from = max(active_from, report_from)
        window_to = min(active_until if active_until is not None else report_to,
                        report_to)
        if window_from >= window_to:
            raise ValueError(f"asset {asset_id} does not intersect the report interval")
        assets[asset_id] = {
            "id": asset_id,
            "category": category,
            "active_from": active_from,
            "active_until": active_until,
            "window_from": window_from,
            "window_to": window_to,
        }

    orders = []
    order_ids = set()
    for raw in _list(data["maintenance_orders"], "maintenance_orders"):
        fields(raw, ("id", "asset_id", "planned_start", "planned_end",
                     "executed_start", "executed_end"))
        order_id = _label(raw["id"], "maintenance_order.id")
        if order_id in order_ids:
            raise ValueError(f"duplicate maintenance order ID: {order_id}")
        order_ids.add(order_id)
        asset_id = _label(raw["asset_id"], "maintenance_order.asset_id")
        asset = assets.get(asset_id)
        if asset is None:
            raise ValueError(
                f"maintenance order {order_id} references unknown asset {asset_id}")
        planned_start = _date(raw["planned_start"], "planned_start")
        planned_end = _date(raw["planned_end"], "planned_end")
        _nonempty(planned_start, planned_end, "planned")
        _bounded(asset, planned_start, planned_end, report_from, report_to,
                 "planned interval")
        executed_start_raw = raw["executed_start"]
        executed_end_raw = raw["executed_end"]
        if (executed_start_raw is None) != (executed_end_raw is None):
            raise ValueError(
                "executed_start and executed_end must both be null or both be dates")
        if executed_start_raw is None:
            executed_start = executed_end = None
        else:
            executed_start = _date(executed_start_raw, "executed_start")
            executed_end = _date(executed_end_raw, "executed_end")
            _nonempty(executed_start, executed_end, "executed")
            _bounded(asset, executed_start, executed_end, report_from, report_to,
                     "executed interval")
        orders.append({
            "id": order_id,
            "asset_id": asset_id,
            "planned_start": planned_start,
            "planned_end": planned_end,
            "executed_start": executed_start,
            "executed_end": executed_end,
        })

    incidents = []
    incident_ids = set()
    for raw in _list(data["incidents"], "incidents"):
        fields(raw, ("id", "asset_id", "start", "end"))
        incident_id = _label(raw["id"], "incident.id")
        if incident_id in incident_ids:
            raise ValueError(f"duplicate incident ID: {incident_id}")
        incident_ids.add(incident_id)
        asset_id = _label(raw["asset_id"], "incident.asset_id")
        asset = assets.get(asset_id)
        if asset is None:
            raise ValueError(f"incident {incident_id} references unknown asset {asset_id}")
        start = _date(raw["start"], "incident.start")
        end = _date(raw["end"], "incident.end")
        _nonempty(start, end, "incident")
        _bounded(asset, start, end, report_from, report_to, "incident interval")
        incidents.append({"id": incident_id, "asset_id": asset_id,
                          "start": start, "end": end})

    return report_from, report_to, assets, orders, incidents
