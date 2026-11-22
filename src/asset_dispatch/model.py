"""Strict validation for the joint maintenance/usage dispatch plan.

The ``availability`` member is a complete ``asset-availability`` input and is
checked by that command's own validator; this module only adds the tighter
dispatch capacity limits and the new ``crew_by_date`` / ``flexible_orders`` /
``requests`` sections.  All intervals stay half-open natural-day ranges.
"""
from datetime import date, timedelta

from asset_availability.model import validate as validate_availability
from asset_schedule.io import fields, integer

MAX_DAYS = 28
MAX_ASSETS = 12
MAX_FLEXIBLE_ORDERS = 10
MAX_REQUESTS = 12
MAX_CREW = 12


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


def _list(value, name, maximum):
    if not isinstance(value, list):
        raise ValueError(f"{name} requires an array")
    if len(value) > maximum:
        raise ValueError(f"{name} allows at most {maximum} rows")
    return value


def _crew_by_date(value, report_from, report_to):
    if not isinstance(value, dict):
        raise ValueError("crew_by_date requires an object")
    crew = {}
    for key, raw in value.items():
        day = _date(key, "crew_by_date key")
        crew[day] = integer(raw, f"crew_by_date[{key}]", 0, MAX_CREW)
    expected = {report_from + timedelta(days=offset)
                for offset in range((report_to - report_from).days)}
    if set(crew) != expected:
        raise ValueError(
            "crew_by_date must contain exactly each day of the planning window")
    return crew


def _flexible_orders(value, assets, fixed_ids, report_from, report_to):
    orders = []
    seen = set()
    for raw in _list(value, "flexible_orders", MAX_FLEXIBLE_ORDERS):
        fields(raw, ("id", "asset_id", "window_start", "window_end",
                     "duration_days", "preferred_start"))
        order_id = _label(raw["id"], "flexible_order.id")
        if order_id in seen:
            raise ValueError(f"duplicate flexible order ID: {order_id}")
        if order_id in fixed_ids:
            raise ValueError(
                f"flexible order ID duplicates a fixed maintenance order: {order_id}")
        seen.add(order_id)
        asset_id = _label(raw["asset_id"], "flexible_order.asset_id")
        if asset_id not in assets:
            raise ValueError(
                f"flexible order {order_id} references unknown asset {asset_id}")
        window_start = _date(raw["window_start"], "window_start")
        window_end = _date(raw["window_end"], "window_end")
        if window_start >= window_end:
            raise ValueError("maintenance window must be nonempty")
        if window_start < report_from or window_end > report_to:
            raise ValueError("maintenance window must lie within the planning window")
        length = (window_end - window_start).days
        duration = integer(raw["duration_days"], "duration_days", 1, length)
        preferred = _date(raw["preferred_start"], "preferred_start")
        if (preferred < window_start
                or preferred + timedelta(days=duration) > window_end):
            raise ValueError(
                "preferred_start must fit the whole duration inside the window")
        orders.append({
            "id": order_id,
            "asset_id": asset_id,
            "window_start": window_start,
            "window_end": window_end,
            "duration_days": duration,
            "preferred_start": preferred,
        })
    return orders


def _requests(value, report_from, report_to):
    requests = []
    seen = set()
    for raw in _list(value, "requests", MAX_REQUESTS):
        fields(raw, ("id", "category", "start", "end", "priority"))
        request_id = _label(raw["id"], "request.id")
        if request_id in seen:
            raise ValueError(f"duplicate request ID: {request_id}")
        seen.add(request_id)
        category = _label(raw["category"], "request.category")
        start = _date(raw["start"], "request.start")
        end = _date(raw["end"], "request.end")
        if start >= end:
            raise ValueError("request interval must be nonempty")
        if start < report_from or end > report_to:
            raise ValueError("request interval must lie within the planning window")
        priority = raw["priority"]
        if priority not in ("high", "normal"):
            raise ValueError('priority must be "high" or "normal"')
        requests.append({
            "id": request_id,
            "category": category,
            "start": start,
            "end": end,
            "high": priority == "high",
        })
    return requests


def validate(data):
    """Return the fully checked dispatch problem as a plain dict."""
    fields(data, ("availability", "crew_by_date", "flexible_orders", "requests"))
    availability = data["availability"]
    if not isinstance(availability, dict):
        raise ValueError("availability requires an object")
    report_from, report_to, assets, fixed_orders, incidents = validate_availability(
        availability)
    span = (report_to - report_from).days
    if span > MAX_DAYS:
        raise ValueError(f"planning window must span 1..{MAX_DAYS} natural days")
    if len(assets) > MAX_ASSETS:
        raise ValueError(f"at most {MAX_ASSETS} assets")
    crew = _crew_by_date(data["crew_by_date"], report_from, report_to)
    fixed_ids = {order["id"] for order in fixed_orders}
    orders = _flexible_orders(data["flexible_orders"], assets, fixed_ids,
                              report_from, report_to)
    requests = _requests(data["requests"], report_from, report_to)
    return {
        "report_from": report_from,
        "report_to": report_to,
        "assets": assets,
        "fixed_orders": fixed_orders,
        "incidents": incidents,
        "crew": crew,
        "orders": orders,
        "requests": requests,
    }
