"""同一套整月折旧与处置规则下的关账流水、重述事实与两种月报口径。"""
from collections import defaultdict

from asset_schedule.model import month, name

from .model import KINDS


def version_entries(asset):
    """一条资产版本按 v1 整月规则应有的全部归属键 -> 有符号金额。

    键为 (经济月序号, 资产 ID, 类别, 金额种类)；经济月是业务应发生的月份，
    与该版本何时被财务已知无关。整月规则与 asset_schedule.depreciation 一致：
    第 i 个使用月累计折旧 = floor((cost-salvage)*i/life)，处置月不计折旧。
    """
    start = month(asset["in_service_month"])
    life = asset["life_months"]
    base = asset["cost_cents"] - asset["salvage_cents"]
    category = asset["category"]
    asset_id = asset["id"]
    entries = {(start, asset_id, category, "cost"): asset["cost_cents"]}
    disposal = asset["disposal"]
    dispose_month = month(disposal["month"]) if disposal else None
    last_dep_month = (start + life - 1) if dispose_month is None else min(start + life - 1, dispose_month - 1)
    for i in range(1, last_dep_month - start + 2):
        before = base * (i - 1) // life
        after = base * i // life
        entries[(start + i - 1, asset_id, category, "depreciation")] = after - before
    if dispose_month is not None:
        dep_months = max(0, min(life, dispose_month - start))
        accumulated = base * dep_months // life
        carrying = asset["cost_cents"] - accumulated
        entries[(dispose_month, asset_id, category, "carrying_removed")] = carrying
        entries[(dispose_month, asset_id, category, "proceeds")] = disposal["proceeds_cents"]
    return entries


def _due_map(entries):
    """到期索引：经济月 -> 该资产版本在该月到期的归属键集合。"""
    due = defaultdict(set)
    for key in entries:
        due[key[0]].add(key)
    return due


def build_journal(first_close, as_of, revisions):
    """逐月纳入新版本并关账，返回 journal 行与最终版本重述归属表。

    每次关账只发布当月流水：对截至当月已知版本，令每个不晚于关账月的归属键
    自首月累计净额等于其应有金额；同键同月一条非零净额。经济月在未来的键
    只登记到期索引，到来前不产生任何行，期间被新版本撤销即无痕。
    """
    by_month = defaultdict(list)
    for known, asset in revisions:
        by_month[known].append(asset)

    current = {}          # asset_id -> 当前版本条目 dict
    due = defaultdict(set)  # 经济月 -> 全部到期键
    posted = {}           # 归属键 -> 已发布累计净额
    rows = []

    for close_month in range(first_close, as_of + 1):
        candidates = set()
        for asset in by_month.get(close_month, ()):
            asset_id = asset["id"]
            new_entries = version_entries(asset)
            if asset_id in current:
                for old_key in current[asset_id]:
                    due[old_key[0]].discard(old_key)
                    candidates.add(old_key)
            current[asset_id] = new_entries
            for eco_month, keys in _due_map(new_entries).items():
                due[eco_month].update(keys)
            candidates.update(new_entries)
        candidates.update(due.get(close_month, ()))

        for key in sorted(candidates):
            # 未来经济月且从未发布过的键只能等待，不能因提前知道事实而入账。
            if key[0] > close_month and key not in posted:
                continue
            expected = 0
            owner = current.get(key[1])
            if owner is not None and key in owner:
                expected = owner[key]
            delta = expected - posted.get(key, 0)
            if delta:
                rows.append({
                    "posted_month": name(close_month),
                    "economic_month": name(key[0]),
                    "asset_id": key[1],
                    "category": key[2],
                    "kind": key[3],
                    "amount_cents": delta,
                })
                posted[key] = expected
    rows.sort(key=lambda row: (row["posted_month"], row["economic_month"],
                               row["asset_id"], row["category"], row["kind"]))

    restated = {}
    for entries in current.values():
        restated.update(entries)
    return rows, restated, current


def _blank_flows():
    return {f"{kind}_cents": 0 for kind in KINDS}


def build_periods(first_close, as_of, rows, restated):
    """按月构造 booked（按 posted_month）与 restated（按经济月、最终版本）两种口径。"""
    booked_flow = defaultdict(lambda: defaultdict(lambda: _blank_flows()))
    restated_flow = defaultdict(lambda: defaultdict(lambda: _blank_flows()))
    for row in rows:
        booked_flow[month(row["posted_month"])][row["category"]][f"{row['kind']}_cents"] += row["amount_cents"]
    for (eco_month, _asset_id, category, kind), amount in restated.items():
        if first_close <= eco_month <= as_of:
            restated_flow[eco_month][category][f"{kind}_cents"] += amount

    booked_cum = defaultdict(int)
    restated_cum = defaultdict(int)
    periods = []
    for close_month in range(first_close, as_of + 1):
        periods.append({
            "month": name(close_month),
            "booked": _basis_block(booked_flow.get(close_month, {}), booked_cum),
            "restated": _basis_block(restated_flow.get(close_month, {}), restated_cum),
        })
    return periods


def _basis_block(month_flows, cumulative):
    block = {}
    for category in sorted(set(month_flows) | set(cumulative)):
        flows = month_flows.get(category, _blank_flows())
        gain_loss = flows["proceeds_cents"] - flows["carrying_removed_cents"]
        cumulative[category] += (flows["cost_cents"] - flows["depreciation_cents"]
                                 - flows["carrying_removed_cents"])
        closing = cumulative[category]
        if any(flows.values()) or closing:
            block[category] = {**flows, "gain_loss_cents": gain_loss,
                               "closing_book_cents": closing}
    return block


def reconcile(rows, restated, as_of):
    """截至同一截止月，两种口径的累计四项金额与期末账面按类别对平，否则报错。"""
    booked_totals = defaultdict(lambda: _blank_flows())
    for row in rows:
        booked_totals[row["category"]][f"{row['kind']}_cents"] += row["amount_cents"]
    restated_totals = defaultdict(lambda: _blank_flows())
    for (eco_month, _asset_id, category, kind), amount in restated.items():
        if eco_month <= as_of:
            restated_totals[category][f"{kind}_cents"] += amount

    categories = sorted(set(booked_totals) | set(restated_totals))
    for category in categories:
        if dict(booked_totals[category]) != dict(restated_totals[category]):
            raise ValueError(f"cumulative flow reconciliation failed for category {category}")
        booked_book = (booked_totals[category]["cost_cents"]
                       - booked_totals[category]["depreciation_cents"]
                       - booked_totals[category]["carrying_removed_cents"])
        restated_book = (restated_totals[category]["cost_cents"]
                         - restated_totals[category]["depreciation_cents"]
                         - restated_totals[category]["carrying_removed_cents"])
        if booked_book != restated_book:
            raise ValueError(f"closing book reconciliation failed for category {category}")
