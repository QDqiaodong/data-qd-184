"""价值承继报告装配：校验、回放、事件明细与月表对账。"""
from asset_schedule.model import name as month_name

from .engine import reconcile, replay
from .model import validate


def run(data):
    from_month, as_of_month, assets, events = validate(data)
    event_rows, periods = replay(from_month, as_of_month, assets, events)
    reconcile(event_rows, periods)
    return {
        "from_month": month_name(from_month),
        "as_of_month": month_name(as_of_month),
        "events": event_rows,
        "periods": periods,
    }
