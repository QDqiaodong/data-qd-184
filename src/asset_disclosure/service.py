"""披露报告装配：台账回放、真值交叉表、最优遮蔽选择与报告成形。"""
from asset_lineage.service import run as run_lineage

from .engine import optimize
from .model import validate


def _truth_table(ledger_rows, categories, custodians, mapping):
    """按截止月设备行自己的类别与归口保管单位汇总期末账面余额。"""
    devices = {row["asset_id"] for row in ledger_rows}
    if set(mapping) != devices:
        raise ValueError(
            "custodian_by_device keys must equal the as-of month device IDs")
    category_set = set(categories)
    truth = {(category, custodian): 0
             for category in categories for custodian in custodians}
    for row in ledger_rows:
        category = row["category"]
        if category not in category_set:
            raise ValueError(f"as-of month category not listed: {category}")
        key = (category, mapping[row["asset_id"]])
        truth[key] += row["closing_book_cents"]
    return truth


def run(data):
    spec = validate(data)
    lineage = run_lineage(data["lineage"])
    as_of_month = lineage["as_of_month"]
    ledger_rows = lineage["periods"][-1]["assets"]
    categories = spec["categories"]
    custodians = spec["custodians"]
    policies = spec["policies"]
    truth = _truth_table(ledger_rows, categories, custodians, spec["mapping"])
    for key, policy in policies.items():
        if policy["mode"] == "structural_zero" and truth[key] != 0:
            raise ValueError(f"structural_zero cell carries a balance: {key}")

    subset = optimize(categories, custodians, truth, policies)
    if subset is None:
        return {"status": "blocked", "as_of_month": as_of_month}

    hidden = {key for key, policy in policies.items()
              if policy["mode"] == "protected"} | subset
    cells = []
    for category in sorted(categories):
        for custodian in sorted(custodians):
            key = (category, custodian)
            if key in hidden:
                cells.append({"category": category, "custodian": custodian,
                              "status": "hidden"})
            else:
                cells.append({"category": category, "custodian": custodian,
                              "status": "shown",
                              "closing_book_cents": truth[key]})
    return {
        "status": "ready",
        "as_of_month": as_of_month,
        "row_totals": [{"category": category,
                        "closing_book_cents": sum(truth[(category, custodian)]
                                                  for custodian in custodians)}
                       for category in sorted(categories)],
        "column_totals": [{"custodian": custodian,
                           "closing_book_cents": sum(truth[(category, custodian)]
                                                     for category in categories)}
                          for custodian in sorted(custodians)],
        "cells": cells,
    }
