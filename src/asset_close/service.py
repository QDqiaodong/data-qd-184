"""关账报告装配：校验、流水、两种月报与截止月对账。"""
from asset_schedule.model import name

from .engine import build_journal, build_periods, reconcile
from .model import validate


def run(data):
    first_close, as_of, revisions = validate(data)
    rows, restated, _current = build_journal(first_close, as_of, revisions)
    reconcile(rows, restated, as_of)
    periods = build_periods(first_close, as_of, rows, restated)
    return {
        "first_close_month": name(first_close),
        "as_of_month": name(as_of),
        "journal": rows,
        "periods": periods,
    }
