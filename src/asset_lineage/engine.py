"""价值承继回放引擎：按月认列投入、按 seq 处理事件、月末对仍在册设备计提。

每台在册设备持有一组价值轨迹，键为 (原始投入 ID, 原应计月序号)，应计月为
None 表示该原始投入的残值。改造只在设备之间移动这些轨迹：合装按同一轨迹键
合并为一笔，拆分对每条轨迹独立按权重分配整数分；轨迹总额在改造前后守恒。
"""
from collections import defaultdict

from asset_schedule.model import month as month_index
from asset_schedule.model import name as month_name

SALVAGE = None  # 轨迹键中的残值标记

METRICS = ("opening_book_cents", "acquired_cents", "transfer_in_cents",
           "transfer_out_cents", "depreciation_cents", "carrying_removed_cents",
           "proceeds_cents", "gain_loss_cents", "closing_book_cents")


def _blank():
    return {key: 0 for key in METRICS}


def origin_tracks(asset):
    """一笔原始投入按 v1 整月直线规则展开的全部价值轨迹（零额轨迹不保留）。"""
    start = month_index(asset["in_service_month"])
    life = asset["life_months"]
    base = asset["cost_cents"] - asset["salvage_cents"]
    origin = asset["id"]
    tracks = {}
    for i in range(1, life + 1):
        amount = base * i // life - base * (i - 1) // life
        if amount:
            tracks[(origin, start + i - 1)] = amount
    if asset["salvage_cents"]:
        tracks[(origin, SALVAGE)] = asset["salvage_cents"]
    return tracks


def allocate(total, targets):
    """把 total 整数分按权重分给各目标：先取精确份额的整数部分，剩余每分
    依份额小数部分从大到小给不同的目标，小数部分相同按目标 ID 字典序。"""
    whole = sum(weight for _, weight in targets)
    shares = {}
    fractions = {}
    for target_id, weight in targets:
        product = total * weight
        shares[target_id] = product // whole
        fractions[target_id] = product % whole
    remainder = total - sum(shares.values())
    order = sorted(targets, key=lambda item: (-fractions[item[0]], item[0]))
    for target_id, _ in order[:remainder]:
        shares[target_id] += 1
    return shares


def _detail(asset_id, tracks):
    by_origin = defaultdict(int)
    for (origin, _sched), amount in tracks.items():
        by_origin[origin] += amount
    return {"asset_id": asset_id,
            "carrying_cents": sum(by_origin.values()),
            "by_origin": {origin: by_origin[origin] for origin in sorted(by_origin)
                          if by_origin[origin]}}


def replay(from_month, as_of_month, assets, events):
    """回放整个报告区间，返回 events 明细行与 periods 月表。"""
    devices = {}   # device_id -> {"category": str, "tracks": dict, "active": bool}
    by_month_events = defaultdict(list)
    for event in events:
        by_month_events[event["month"]].append(event)
    by_month_assets = defaultdict(list)
    for asset in assets.values():
        by_month_assets[month_index(asset["in_service_month"])].append(asset)

    event_rows = []
    periods = []
    for current in range(from_month, as_of_month + 1):
        metrics = {}

        def flows(device_id):
            return metrics.setdefault(device_id, _blank())

        # 期初：上月末仍在册的设备（含零值设备）进入本月台账。
        for device_id, device in devices.items():
            if device["active"]:
                flows(device_id)["opening_book_cents"] = sum(device["tracks"].values())

        # 先认列该月原始投入。
        for asset in sorted(by_month_assets.get(current, ()), key=lambda a: a["id"]):
            devices[asset["id"]] = {"category": asset["category"],
                                    "tracks": origin_tracks(asset), "active": True}
            flows(asset["id"])["acquired_cents"] += asset["cost_cents"]

        # 再按 seq 依次处理事件；新设备立即可用于下一笔，注销设备不能再用。
        for event in sorted(by_month_events.get(current, ()), key=lambda e: e["seq"]):
            if event["kind"] == "reconfigure":
                event_rows.append(_apply_reconfigure(event, devices, flows))
            else:
                event_rows.append(_apply_sell(event, devices, flows))

        # 最后只给仍在册的设备计提该月折旧（按轨迹的原应计月到期）。
        for device_id, device in devices.items():
            if not device["active"]:
                continue
            tracks = device["tracks"]
            due = [key for key in tracks if key[1] == current]
            depreciation = sum(tracks.pop(key) for key in due)
            flows(device_id)["depreciation_cents"] += depreciation

        # 期末结平并校验：期末 = 期初 + 投入 + 转入 - 转出 - 折旧 - 处置转出。
        asset_rows = []
        for device_id in sorted(metrics):
            row = metrics[device_id]
            row["closing_book_cents"] = (row["opening_book_cents"] + row["acquired_cents"]
                                         + row["transfer_in_cents"] - row["transfer_out_cents"]
                                         - row["depreciation_cents"]
                                         - row["carrying_removed_cents"])
            device = devices[device_id]
            expected = sum(device["tracks"].values()) if device["active"] else 0
            if row["closing_book_cents"] != expected:
                raise ValueError(f"closing book mismatch for device {device_id}")
            asset_rows.append({"asset_id": device_id, "category": device["category"], **row})

        categories = {}
        for row in asset_rows:
            total = categories.setdefault(row["category"], _blank())
            for key in METRICS:
                total[key] += row[key]
        if sum(row["transfer_in_cents"] for row in asset_rows) \
                != sum(row["transfer_out_cents"] for row in asset_rows):
            raise ValueError("warehouse transfer in/out mismatch")
        periods.append({"month": month_name(current), "assets": asset_rows,
                        "categories": {key: categories[key] for key in sorted(categories)}})

    return event_rows, periods


def _apply_reconfigure(event, devices, flows):
    sources = []
    merged = defaultdict(int)
    for source_id in event["sources"]:
        device = devices.get(source_id)
        if device is None or not device["active"]:
            raise ValueError(
                f"event {event['id']} references a device not on the books: {source_id}")
        for key, amount in device["tracks"].items():
            merged[key] += amount
    for source_id in event["sources"]:
        device = devices[source_id]
        flows(source_id)["transfer_out_cents"] += sum(device["tracks"].values())
        sources.append(_detail(source_id, device["tracks"]))
        device["tracks"] = {}
        device["active"] = False
    sources.sort(key=lambda row: row["asset_id"])

    weights = [(target["id"], target["weight"]) for target in event["targets"]]
    allocated = {target["id"]: {} for target in event["targets"]}
    for key, total in merged.items():
        for target_id, share in allocate(total, weights).items():
            if share:
                allocated[target_id][key] = allocated[target_id].get(key, 0) + share
    targets = []
    for target in event["targets"]:
        target_id = target["id"]
        tracks = allocated[target_id]
        devices[target_id] = {"category": target["category"], "tracks": tracks,
                              "active": True}
        flows(target_id)["transfer_in_cents"] += sum(tracks.values())
        targets.append(_detail(target_id, tracks))
    targets.sort(key=lambda row: row["asset_id"])
    return {"month": month_name(event["month"]), "seq": event["seq"], "id": event["id"],
            "kind": "reconfigure", "sources": sources, "targets": targets}


def _apply_sell(event, devices, flows):
    asset_id = event["asset_id"]
    device = devices.get(asset_id)
    if device is None or not device["active"]:
        raise ValueError(
            f"event {event['id']} references a device not on the books: {asset_id}")
    asset = _detail(asset_id, device["tracks"])
    proceeds = event["proceeds_cents"]
    gain_loss = proceeds - asset["carrying_cents"]
    flows(asset_id)["carrying_removed_cents"] += asset["carrying_cents"]
    flows(asset_id)["proceeds_cents"] += proceeds
    flows(asset_id)["gain_loss_cents"] += gain_loss
    device["tracks"] = {}
    device["active"] = False
    return {"month": month_name(event["month"]), "seq": event["seq"], "id": event["id"],
            "kind": "sell", "asset": asset, "proceeds_cents": proceeds,
            "gain_loss_cents": gain_loss}


def reconcile(event_rows, periods):
    """月表转入、转出和处置金额须与事件明细逐设备对平，否则报错。"""
    per_month = defaultdict(lambda: defaultdict(_blank))
    for row in event_rows:
        month = row["month"]
        if row["kind"] == "reconfigure":
            for source in row["sources"]:
                per_month[month][source["asset_id"]]["transfer_out_cents"] += \
                    source["carrying_cents"]
            for target in row["targets"]:
                per_month[month][target["asset_id"]]["transfer_in_cents"] += \
                    target["carrying_cents"]
        else:
            flows = per_month[month][row["asset"]["asset_id"]]
            flows["carrying_removed_cents"] += row["asset"]["carrying_cents"]
            flows["proceeds_cents"] += row["proceeds_cents"]
            flows["gain_loss_cents"] += row["gain_loss_cents"]
    for period in periods:
        expected = per_month.get(period["month"], {})
        listed = {row["asset_id"] for row in period["assets"]}
        if not set(expected) <= listed:
            raise ValueError(f"event device missing from ledger in {period['month']}")
        for row in period["assets"]:
            device_id = row["asset_id"]
            flows = expected.get(device_id, _blank())
            for key in ("transfer_in_cents", "transfer_out_cents",
                        "carrying_removed_cents", "proceeds_cents", "gain_loss_cents"):
                if row[key] != flows[key]:
                    raise ValueError(
                        f"event/period mismatch for device {device_id} in {period['month']}")
        transfer_in = sum(row["transfer_in_cents"] for row in period["assets"])
        transfer_out = sum(row["transfer_out_cents"] for row in period["assets"])
        if transfer_in != transfer_out:
            raise ValueError(f"warehouse transfer in/out mismatch in {period['month']}")
