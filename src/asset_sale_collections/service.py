"""收款催收报告装配：校验输入（含交接包复核）并按月回放。"""
from asset_schedule.model import name as month_name

from .engine import run_batch
from .model import validate


def run(data):
    validated = validate(data)
    periods, checkpoint = run_batch(validated)
    return {
        "through_month": month_name(validated["through"]),
        "periods": periods,
        "checkpoint": checkpoint,
    }
