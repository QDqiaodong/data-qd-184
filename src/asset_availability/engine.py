"""Date-level asset state, maintenance fulfilment and category/month rollups.

All intervals in this module are half-open natural-day ranges represented by
integer ordinals.  Each reporting set (planned maintenance, executed
maintenance, incidents) is merged before it is measured, so overlapping rows
can never double-count a day.
"""
from bisect import bisect_right
from datetime import date

COUNTERS = ("active_days", "available_days", "maintenance_days", "incident_days",
            "unplanned_incident_days", "planned_maintenance_days",
            "executed_maintenance_days")


def _iso(ordinal):
    return date.fromordinal(ordinal).isoformat()


def _clip(intervals, low, high):
    clipped = []
    for start, end in intervals:
        left = max(start, low)
        right = min(end, high)
        if left < right:
            clipped.append((left, right))
    return clipped


def _union(intervals):
    """Sort and merge overlapping or touching half-open intervals."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _subtract(intervals, holes):
    """Return merged *intervals* minus the union of merged *holes*.

    Both arguments are sorted and internally disjoint, so a single sweep is
    enough even for tens of thousands of rows.
    """
    remaining = []
    cursor = 0
    for start, end in intervals:
        while cursor < len(holes) and holes[cursor][1] <= start:
            cursor += 1
        position = start
        index = cursor
        while index < len(holes):
            hole_start, hole_end = holes[index]
            if hole_start >= end:
                break
            if hole_start > position:
                remaining.append((position, hole_start))
            position = max(position, hole_end)
            if position >= end:
                break
            index += 1
        if position < end:
            remaining.append((position, end))
    return remaining


def _merge_states(blocks):
    """Combine disjoint ``(status, intervals)`` blocks into coalesced segments."""
    pieces = sorted((start, end, status) for status, intervals in blocks
                    for start, end in intervals)
    segments = []
    for start, end, status in pieces:
        if segments and segments[-1][2] == status and segments[-1][1] == start:
            segments[-1] = (segments[-1][0], end, status)
        else:
            segments.append((start, end, status))
    return segments


def order_metrics(order, report_to):
    """Fulfilment facts for one order, independent of every other order.

    An unexecuted order whose planned window still reaches the report end
    boundary is ``pending``; once its whole window lies strictly before that
    boundary the order is ``missed``.
    """
    planned_days = (order["planned_end"] - order["planned_start"]).days
    if order["executed_start"] is None:
        executed_days = late = outside = 0
        status = "pending" if order["planned_end"] >= report_to else "missed"
    else:
        executed_days = (order["executed_end"] - order["executed_start"]).days
        first_start = order["executed_start"].toordinal()
        first_end = order["executed_end"].toordinal()
        second_start = order["planned_start"].toordinal()
        second_end = order["planned_end"].toordinal()
        overlap = max(0, min(first_end, second_end) - max(first_start, second_start))
        outside = executed_days - overlap
        late = max(0, (order["executed_start"] - order["planned_start"]).days)
        within = (order["planned_start"] <= order["executed_start"]
                  and order["executed_end"] <= order["planned_end"])
        status = "completed_within_window" if within else "completed_outside_window"
    return {
        "id": order["id"],
        "asset_id": order["asset_id"],
        "planned_start": order["planned_start"].isoformat(),
        "planned_end": order["planned_end"].isoformat(),
        "executed_start": (None if order["executed_start"] is None
                           else order["executed_start"].isoformat()),
        "executed_end": (None if order["executed_end"] is None
                         else order["executed_end"].isoformat()),
        "planned_days": planned_days,
        "executed_days": executed_days,
        "late_start_days": late,
        "outside_plan_days": outside,
        "status": status,
    }


def _month_spans(report_from, report_to):
    """Natural months intersecting ``[report_from, report_to)``, ascending."""
    spans = []
    cursor = date(report_from.year, report_from.month, 1)
    while cursor < report_to:
        if cursor.month == 12:
            next_month = (date(cursor.year + 1, 1, 1)
                          if cursor.year < 9999 else report_to)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        spans.append((max(cursor, report_from).toordinal(),
                      min(next_month, report_to).toordinal(),
                      cursor.year * 12 + cursor.month - 1,
                      f"{cursor.year:04d}-{cursor.month:02d}"))
        cursor = next_month
    return spans


def _add_by_month(spans, starts, interval, totals):
    """Add the length of *interval* to each intersecting month's counter."""
    start, end = interval
    index = max(0, bisect_right(starts, start) - 1)
    while index < len(spans) and spans[index][0] < end:
        month_start, month_end, key, _name = spans[index]
        left = max(start, month_start)
        right = min(end, month_end)
        if left < right:
            totals[key] = totals.get(key, 0) + (right - left)
        index += 1


def _group(rows, key):
    grouped = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


def build_details(report_from, report_to, assets, orders, incidents):
    """Per-asset merged segments and per-(month, category) day counters."""
    spans = _month_spans(report_from, report_to)
    starts = [span[0] for span in spans]
    orders_by_asset = _group(orders, "asset_id")
    incidents_by_asset = _group(incidents, "asset_id")

    details = []
    monthly = {}
    for asset_id in sorted(assets):
        asset = assets[asset_id]
        low = asset["window_from"].toordinal()
        high = asset["window_to"].toordinal()
        asset_orders = orders_by_asset.get(asset_id, ())

        planned = _union(_clip([(o["planned_start"].toordinal(),
                                 o["planned_end"].toordinal())
                                for o in asset_orders], low, high))
        executed = _union(_clip([(o["executed_start"].toordinal(),
                                  o["executed_end"].toordinal())
                                 for o in asset_orders
                                 if o["executed_start"] is not None], low, high))
        incident_spans = _union(_clip([(i["start"].toordinal(), i["end"].toordinal())
                                       for i in incidents_by_asset.get(asset_id, ())],
                                      low, high))

        maintained = _union(planned + executed)
        occupied = _union(maintained + incident_spans)
        available = _subtract([(low, high)], occupied)
        maintenance_only = _subtract(maintained, incident_spans)
        unplanned = _subtract(incident_spans, planned)

        counters = {name: {} for name in COUNTERS}
        _add_by_month(spans, starts, (low, high), counters["active_days"])
        for name, intervals in (("available_days", available),
                                ("maintenance_days", maintenance_only),
                                ("incident_days", incident_spans),
                                ("unplanned_incident_days", unplanned),
                                ("planned_maintenance_days", planned),
                                ("executed_maintenance_days", executed)):
            for interval in intervals:
                _add_by_month(spans, starts, interval, counters[name])

        for key in counters["active_days"]:
            values = [counters[name].get(key, 0) for name in COUNTERS]
            slot = monthly.setdefault((key, asset["category"]), [0] * len(COUNTERS))
            for position, value in enumerate(values):
                slot[position] += value

        segments = _merge_states((("incident", incident_spans),
                                  ("maintenance", maintenance_only),
                                  ("available", available)))
        details.append({
            "id": asset["id"],
            "category": asset["category"],
            "active_from": asset["active_from"].isoformat(),
            "active_until": (None if asset["active_until"] is None
                             else asset["active_until"].isoformat()),
            "segments": [{"start": _iso(start), "end": _iso(end), "status": status}
                         for start, end, status in segments],
        })

    month_names = {key: name for _start, _end, key, name in spans}
    return details, monthly, month_names


def _periods(month_names, monthly):
    grouped = {}
    for (month, category), values in monthly.items():
        block = dict(zip(COUNTERS, values))
        if any(block.values()):
            grouped.setdefault(month, {})[category] = block
    return [{"month": month_names[key],
             "categories": dict(sorted(grouped.get(key, {}).items()))}
            for key in sorted(month_names)]


def report(data):
    from .model import validate

    report_from, report_to, assets, orders, incidents = validate(data)
    details, monthly, month_names = build_details(report_from, report_to,
                                                  assets, orders, incidents)
    periods = _periods(month_names, monthly)
    totals = {name: 0 for name in COUNTERS}
    for period in periods:
        for values in period["categories"].values():
            for name in COUNTERS:
                totals[name] += values[name]
    return {
        "from_date": report_from.isoformat(),
        "to_date": report_to.isoformat(),
        "assets": details,
        "maintenance_orders": sorted((order_metrics(order, report_to)
                                      for order in orders),
                                     key=lambda row: row["id"]),
        "periods": periods,
        "summary": {
            "assets": len(details),
            "orders": len(orders),
            "incidents": len(incidents),
            "active_days": totals["active_days"],
            "available_days": totals["available_days"],
            "maintenance_days": totals["maintenance_days"],
            "incident_days": totals["incident_days"],
            "unplanned_incident_days": totals["unplanned_incident_days"],
        },
    }
