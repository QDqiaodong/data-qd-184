import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from asset_lineage.engine import allocate, reconcile, replay
from asset_lineage.model import validate
from asset_lineage.service import run

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

METRICS = ("opening_book_cents", "acquired_cents", "transfer_in_cents",
           "transfer_out_cents", "depreciation_cents", "carrying_removed_cents",
           "proceeds_cents", "gain_loss_cents", "closing_book_cents")


def make_asset(asset_id="dev", category="equipment", cost=100, salvage=0, life=3,
               start="2026-01"):
    return {"id": asset_id, "category": category, "cost_cents": cost,
            "salvage_cents": salvage, "life_months": life,
            "in_service_month": start, "disposal": None}


def reconfigure(event_id, month, seq, sources, targets):
    return {"id": event_id, "month": month, "seq": seq, "kind": "reconfigure",
            "sources": sources,
            "targets": [{"id": tid, "category": cat, "weight": w}
                        for tid, cat, w in targets]}


def sell(event_id, month, seq, asset_id, proceeds):
    return {"id": event_id, "month": month, "seq": seq, "kind": "sell",
            "asset_id": asset_id, "proceeds_cents": proceeds}


def lineage_input(assets, events, first="2026-01", as_of="2026-04"):
    return {"from_month": first, "as_of_month": as_of, "assets": assets,
            "events": events}


def acceptance_input(as_of="2026-04", with_sell=True):
    """任务书给出的验收业务事实：A 拆 L/R，与 B 合装 M，再拆 X/Y，3 月出售 X。"""
    assets = [make_asset("A", "equipment", cost=10, salvage=1, life=4, start="2026-01"),
              make_asset("B", "tools", cost=5, salvage=2, life=3, start="2026-02")]
    events = [
        reconfigure("ev-split-a", "2026-02", 1, ["A"],
                    [("L", "equipment", 1), ("R", "equipment", 1)]),
        reconfigure("ev-merge", "2026-02", 2, ["L", "R", "B"],
                    [("M", "equipment", 1)]),
        reconfigure("ev-split-m", "2026-02", 3, ["M"],
                    [("X", "equipment", 1), ("Y", "equipment", 1)]),
    ]
    if with_sell:
        events.append(sell("ev-sell-x", "2026-03", 1, "X", 8))
    return lineage_input(assets, events, as_of=as_of)


def event_by_id(report, event_id):
    return next(row for row in report["events"] if row["id"] == event_id)


def period(report, month):
    return next(row for row in report["periods"] if row["month"] == month)


def device_row(report, month, asset_id):
    return next(row for row in period(report, month)["assets"]
                if row["asset_id"] == asset_id)


def detail_by_id(details, asset_id):
    return next(row for row in details if row["asset_id"] == asset_id)


class AcceptanceExampleTests(unittest.TestCase):
    """任务书给出的完整业务例子，逐笔手工预期值。"""

    def setUp(self):
        self.report = run(acceptance_input())

    def test_first_split_allocates_per_origin_track_with_id_tiebreak(self):
        event = event_by_id(self.report, "ev-split-a")
        self.assertEqual(event["sources"],
                         [{"asset_id": "A", "carrying_cents": 8, "by_origin": {"A": 8}}])
        # A 的 4 月折旧 3 与残值 1 各余 1 分，并列时字典序较小的 L 先拿。
        self.assertEqual(detail_by_id(event["targets"], "L"),
                         {"asset_id": "L", "carrying_cents": 5, "by_origin": {"A": 5}})
        self.assertEqual(detail_by_id(event["targets"], "R"),
                         {"asset_id": "R", "carrying_cents": 3, "by_origin": {"A": 3}})

    def test_merge_combines_same_origin_same_month_shares(self):
        event = event_by_id(self.report, "ev-merge")
        self.assertEqual([row["asset_id"] for row in event["sources"]], ["B", "L", "R"])
        self.assertEqual(event["targets"],
                         [{"asset_id": "M", "carrying_cents": 13,
                           "by_origin": {"A": 8, "B": 5}}])

    def test_second_split_keeps_origins_separate(self):
        event = event_by_id(self.report, "ev-split-m")
        # 若把 L、R 留下的同源份额分开再取整，X 会多拿 2 月属于 A 的折旧（A 变 6）；
        # 若不分 A、B，X 会少拿 4 月对应的 1 分（合计变 8）。
        self.assertEqual(detail_by_id(event["targets"], "X"),
                         {"asset_id": "X", "carrying_cents": 9,
                          "by_origin": {"A": 5, "B": 4}})
        self.assertEqual(detail_by_id(event["targets"], "Y"),
                         {"asset_id": "Y", "carrying_cents": 4,
                          "by_origin": {"A": 3, "B": 1}})

    def test_month_two_depreciation_and_closing(self):
        feb = period(self.report, "2026-02")
        self.assertEqual([row["asset_id"] for row in feb["assets"]],
                         ["A", "B", "L", "M", "R", "X", "Y"])
        x = device_row(self.report, "2026-02", "X")
        y = device_row(self.report, "2026-02", "Y")
        self.assertEqual((x["transfer_in_cents"], x["depreciation_cents"],
                          x["closing_book_cents"]), (9, 2, 7))
        self.assertEqual((y["transfer_in_cents"], y["depreciation_cents"],
                          y["closing_book_cents"]), (4, 1, 3))
        # 改造不计作投入或处置；全仓转入等于转出。
        self.assertEqual(sum(row["acquired_cents"] for row in feb["assets"]), 5)
        self.assertEqual(sum(row["carrying_removed_cents"] for row in feb["assets"]), 0)
        self.assertEqual(sum(row["transfer_in_cents"] for row in feb["assets"]),
                         sum(row["transfer_out_cents"] for row in feb["assets"]))

    def test_sell_removes_all_remaining_value_before_depreciation(self):
        event = event_by_id(self.report, "ev-sell-x")
        self.assertEqual(event["asset"], {"asset_id": "X", "carrying_cents": 7,
                                          "by_origin": {"A": 4, "B": 3}})
        self.assertEqual(event["proceeds_cents"], 8)
        self.assertEqual(event["gain_loss_cents"], 1)
        x = device_row(self.report, "2026-03", "X")
        self.assertEqual((x["depreciation_cents"], x["carrying_removed_cents"],
                          x["closing_book_cents"]), (0, 7, 0))

    def test_y_continues_original_schedule_after_sale(self):
        march = device_row(self.report, "2026-03", "Y")
        april = device_row(self.report, "2026-04", "Y")
        self.assertEqual((march["depreciation_cents"], march["closing_book_cents"]), (1, 2))
        self.assertEqual((april["depreciation_cents"], april["closing_book_cents"]), (1, 1))

    def test_categories_aggregate_asset_rows(self):
        feb = period(self.report, "2026-02")["categories"]
        self.assertEqual(set(feb), {"equipment", "tools"})
        self.assertEqual(feb["equipment"]["transfer_in_cents"], 34)
        self.assertEqual(feb["equipment"]["transfer_out_cents"], 29)
        self.assertEqual(feb["equipment"]["closing_book_cents"], 10)
        self.assertEqual(feb["tools"]["acquired_cents"], 5)
        self.assertEqual(feb["tools"]["transfer_out_cents"], 5)
        march = period(self.report, "2026-03")["categories"]["equipment"]
        self.assertEqual((march["carrying_removed_cents"], march["proceeds_cents"],
                          march["gain_loss_cents"]), (7, 8, 1))


class PrefixInvarianceTests(unittest.TestCase):
    def test_truncated_report_keeps_identical_older_months(self):
        full = run(acceptance_input())
        prefix = run(acceptance_input(as_of="2026-02", with_sell=False))
        self.assertEqual(prefix["events"], full["events"][:3])
        self.assertEqual(prefix["periods"], full["periods"][:2])

    def test_book_value_includes_amounts_due_after_cutoff(self):
        prefix = run(acceptance_input(as_of="2026-02", with_sell=False))
        # 2 月期末 X 的 7 分含尚未到期的 3、4 月折旧与残值，不以报告月截断。
        x = device_row(prefix, "2026-02", "X")
        self.assertEqual(x["closing_book_cents"], 7)
        sell_event = event_by_id(run(acceptance_input()), "ev-sell-x")
        self.assertEqual(sell_event["asset"]["carrying_cents"], 7)

    def test_input_array_order_does_not_change_result(self):
        data = acceptance_input()
        shuffled = lineage_input(list(reversed(data["assets"])),
                                 list(reversed(data["events"])))
        self.assertEqual(run(data), run(shuffled))


class AllocationTests(unittest.TestCase):
    def test_distinct_fractions_rank_remainder_cents(self):
        # 10 按 1:2:4 分：整数部分 1/2/5，余 2 分按小数部分 6/5/3 给 t2、t3。
        shares = allocate(10, [("t1", 1), ("t2", 2), ("t3", 4)])
        self.assertEqual(shares, {"t1": 1, "t2": 3, "t3": 6})

    def test_tied_fractions_fall_back_to_id_order_not_input_order(self):
        shares = allocate(10, [("c-dev", 1), ("a-dev", 1), ("b-dev", 1)])
        self.assertEqual(shares, {"a-dev": 4, "b-dev": 3, "c-dev": 3})

    def test_unequal_weight_split_event_details(self):
        data = lineage_input(
            [make_asset("P", cost=10, salvage=0, life=1)],
            [reconfigure("ev", "2026-01", 1, ["P"],
                         [("t1", "equipment", 1), ("t2", "equipment", 2),
                          ("t3", "equipment", 4)])],
            as_of="2026-01")
        event = run(data)["events"][0]
        got = {row["asset_id"]: row["carrying_cents"] for row in event["targets"]}
        self.assertEqual(got, {"t1": 1, "t2": 3, "t3": 6})
        # 三个目标当月即计提各自份额，全仓折旧守恒。
        jan = period(run(data), "2026-01")
        self.assertEqual(sum(row["depreciation_cents"] for row in jan["assets"]), 10)

    def test_each_origin_track_split_independently_conserves_total(self):
        assets = [make_asset("A", cost=10, salvage=1, life=4),
                  make_asset("B", cost=5, salvage=2, life=3, start="2026-02")]
        merge = reconfigure("ev-merge", "2026-02", 1, ["A", "B"], [("M", "equipment", 1)])
        split = reconfigure("ev-split", "2026-02", 2, ["M"],
                            [("X", "equipment", 3), ("Y", "equipment", 1),
                             ("Z", "equipment", 2)])
        report = run(lineage_input(assets, [merge, split], as_of="2026-02"))
        event = event_by_id(report, "ev-split")
        moved = sum(row["carrying_cents"] for row in event["targets"])
        self.assertEqual(moved, event["sources"][0]["carrying_cents"])
        for row in event["targets"]:
            self.assertEqual(sum(row["by_origin"].values()), row["carrying_cents"])


class BeyondCutoffAndSalvageTests(unittest.TestCase):
    def test_amounts_due_after_report_cutoff_stay_on_the_books(self):
        # 成本 12、残值 2、寿命 6：月折旧 1/2/2/1/2/2。
        asset = make_asset("D", cost=12, salvage=2, life=6)
        split = reconfigure("ev", "2026-02", 1, ["D"],
                            [("E", "equipment", 1), ("F", "equipment", 1)])
        short = run(lineage_input([asset], [split], as_of="2026-02"))
        event = event_by_id(short, "ev")
        self.assertEqual(detail_by_id(event["targets"], "E")["carrying_cents"], 6)
        self.assertEqual(detail_by_id(event["targets"], "F")["carrying_cents"], 5)
        self.assertEqual(device_row(short, "2026-02", "E")["closing_book_cents"], 5)
        self.assertEqual(device_row(short, "2026-02", "F")["closing_book_cents"], 4)

        # 同一原文报告到寿命结束：未到期部分按原应计月逐月计提，残值挂账。
        full = run(lineage_input([asset], [split], as_of="2026-06"))
        for month, dep in (("2026-03", 1), ("2026-04", 1), ("2026-05", 1), ("2026-06", 1)):
            self.assertEqual(device_row(full, month, "E")["depreciation_cents"], dep)
        self.assertEqual(device_row(full, "2026-06", "E")["closing_book_cents"], 1)
        self.assertEqual(device_row(full, "2026-06", "F")["closing_book_cents"], 1)

    def test_fully_depreciated_device_keeps_salvage_and_stays_listed(self):
        # 成本 7、残值 3、寿命 2：1、2 月各提 2，之后只挂残值 3。
        g1 = make_asset("G1", cost=7, salvage=3, life=2)
        g2 = make_asset("G2", cost=5, salvage=2, life=1)
        events = [reconfigure("ev-move", "2026-04", 1, ["G1"], [("H", "equipment", 1)]),
                  sell("ev-sell", "2026-03", 1, "G2", 1)]
        report = run(lineage_input([g1, g2], events))
        march_g1 = device_row(report, "2026-03", "G1")
        self.assertEqual((march_g1["opening_book_cents"], march_g1["depreciation_cents"],
                          march_g1["closing_book_cents"]), (3, 0, 3))
        moved = event_by_id(report, "ev-move")["targets"][0]
        self.assertEqual(moved, {"asset_id": "H", "carrying_cents": 3,
                                 "by_origin": {"G1": 3}})
        # 寿命已完的设备出售：转出全部残值，损益可为负。
        sold = event_by_id(report, "ev-sell")
        self.assertEqual((sold["asset"]["carrying_cents"], sold["gain_loss_cents"]), (2, -1))
        feb_g2 = device_row(report, "2026-02", "G2")
        self.assertEqual((feb_g2["opening_book_cents"], feb_g2["closing_book_cents"]), (2, 2))


class ChainingAndRetirementTests(unittest.TestCase):
    def test_same_month_chaining_allowed(self):
        asset = make_asset("A", cost=9, salvage=0, life=3)
        events = [reconfigure("ev1", "2026-02", 1, ["A"], [("T1", "equipment", 1)]),
                  reconfigure("ev2", "2026-02", 2, ["T1"], [("T2", "equipment", 1)])]
        report = run(lineage_input([asset], events, as_of="2026-03"))
        t2 = device_row(report, "2026-02", "T2")
        self.assertEqual((t2["transfer_in_cents"], t2["depreciation_cents"],
                          t2["closing_book_cents"]), (6, 3, 3))
        # 零值后仍在册：3 月提完最后 3 分，期末 0，仍列入月表。
        t2_march = device_row(report, "2026-03", "T2")
        self.assertEqual((t2_march["depreciation_cents"], t2_march["closing_book_cents"]),
                         (3, 0))

    def _assert_rejected(self, events, fragment="not on the books"):
        data = lineage_input([make_asset("A", cost=9, salvage=0, life=3)], events)
        with self.assertRaisesRegex(ValueError, fragment):
            run(data)

    def test_retired_device_cannot_be_used_later_same_month(self):
        self._assert_rejected([
            reconfigure("ev1", "2026-02", 1, ["A"], [("T1", "equipment", 1)]),
            reconfigure("ev2", "2026-02", 2, ["A"], [("T2", "equipment", 1)])])

    def test_sold_device_cannot_be_reconfigured(self):
        self._assert_rejected([
            sell("ev1", "2026-02", 1, "A", 5),
            reconfigure("ev2", "2026-02", 2, ["A"], [("T1", "equipment", 1)])])

    def test_reconfigured_device_cannot_be_sold(self):
        self._assert_rejected([
            reconfigure("ev1", "2026-02", 1, ["A"], [("T1", "equipment", 1)]),
            sell("ev2", "2026-02", 2, "A", 5)])

    def test_unknown_device_reference_rejected(self):
        self._assert_rejected([sell("ev1", "2026-02", 1, "ghost", 5)])

    def test_device_not_yet_activated_cannot_be_source(self):
        self._assert_rejected([
            reconfigure("ev1", "2026-02", 1, ["T1"], [("T2", "equipment", 1)]),
            reconfigure("ev2", "2026-02", 2, ["A"], [("T1", "equipment", 1)])])

    def test_device_not_yet_recognized_cannot_be_sold(self):
        data = lineage_input([make_asset("A", start="2026-03")],
                             [sell("ev1", "2026-02", 1, "A", 5)])
        with self.assertRaisesRegex(ValueError, "not on the books"):
            run(data)


class CrossCategoryReconciliationTests(unittest.TestCase):
    def test_transfer_moves_value_between_categories_and_ties_out(self):
        asset = make_asset("A", category="old", cost=10, salvage=0, life=2)
        move = reconfigure("ev", "2026-02", 1, ["A"], [("B", "new", 1)])
        report = run(lineage_input([asset], [move], as_of="2026-02"))
        feb = period(report, "2026-02")["categories"]
        self.assertEqual(feb["old"]["transfer_out_cents"], 5)
        self.assertEqual(feb["old"]["closing_book_cents"], 0)
        self.assertEqual(feb["new"]["transfer_in_cents"], 5)
        self.assertEqual(feb["new"]["depreciation_cents"], 5)
        self.assertEqual(feb["new"]["closing_book_cents"], 0)
        for row in period(report, "2026-02")["assets"]:
            self.assertEqual(
                row["closing_book_cents"],
                row["opening_book_cents"] + row["acquired_cents"]
                + row["transfer_in_cents"] - row["transfer_out_cents"]
                - row["depreciation_cents"] - row["carrying_removed_cents"])
        warehouse = period(report, "2026-02")["assets"]
        self.assertEqual(sum(r["transfer_in_cents"] for r in warehouse),
                         sum(r["transfer_out_cents"] for r in warehouse))
        # 1 月只有 old 类别；类别按当月资产行汇总。
        self.assertEqual(set(period(report, "2026-01")["categories"]), {"old"})


class V1ConsistencyTests(unittest.TestCase):
    """不改造时，本命令的折旧与处置口径与 asset-schedule 完全一致。"""

    def test_depreciation_and_disposal_match_v1_schedule(self):
        from asset_schedule.depreciation import schedule as v1_schedule
        from asset_schedule.model import month as v1_month

        a1 = make_asset("a1", "equipment", cost=100, salvage=0, life=3, start="2026-01")
        a2 = make_asset("a2", "tools", cost=200, salvage=20, life=5, start="2026-02")
        events = [sell("ev-sell", "2026-04", 1, "a2", 150)]
        report = run(lineage_input([a1, a2], events, as_of="2026-05"))

        disposals = {"a1": None, "a2": {"month": "2026-04", "proceeds_cents": 150}}
        months = ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05")
        for asset in (a1, a2):
            asset_id = asset["id"]
            v1 = v1_schedule({**asset, "disposal": disposals[asset_id]},
                             v1_month("2026-05"))
            rows = []
            for name in months:
                rows.extend(row for row in period(report, name)["assets"]
                            if row["asset_id"] == asset_id)
            dep = [row["depreciation_cents"] for row in rows if row["depreciation_cents"]]
            self.assertEqual(dep, [row["depreciation_cents"] for row in v1["monthly"]])
            self.assertEqual(sum(dep), v1["accumulated_cents"])
            for monthly in v1["monthly"]:
                row = device_row(report, monthly["month"], asset_id)
                self.assertEqual(row["closing_book_cents"], monthly["closing_book_cents"])
            if v1["disposal_result"]:
                removed = sum(row["carrying_removed_cents"] for row in rows)
                self.assertEqual(removed, v1["disposal_result"]["carrying_cents"])
                gain = sum(row["gain_loss_cents"] for row in rows)
                self.assertEqual(gain, v1["disposal_result"]["gain_loss_cents"])
            else:
                self.assertEqual(rows[-1]["closing_book_cents"], v1["closing_book_cents"])
        # 提完寿命的零值设备仍在册并列示。
        self.assertEqual(device_row(report, "2026-05", "a1")["closing_book_cents"], 0)


class ZeroValueAndEmptyTests(unittest.TestCase):
    def test_zero_value_devices_stay_on_the_books_and_keep_details(self):
        asset = make_asset("Z", cost=0, salvage=0, life=2)
        events = [reconfigure("ev-split", "2026-01", 1, ["Z"],
                              [("Z1", "equipment", 1), ("Z2", "equipment", 1)]),
                  sell("ev-sell", "2026-02", 1, "Z1", 5)]
        report = run(lineage_input([asset], events, as_of="2026-02"))
        event = event_by_id(report, "ev-split")
        self.assertEqual(event["sources"],
                         [{"asset_id": "Z", "carrying_cents": 0, "by_origin": {}}])
        self.assertEqual([row["carrying_cents"] for row in event["targets"]], [0, 0])
        jan_ids = [row["asset_id"] for row in period(report, "2026-01")["assets"]]
        self.assertEqual(jan_ids, ["Z", "Z1", "Z2"])
        sold = event_by_id(report, "ev-sell")
        self.assertEqual((sold["asset"]["carrying_cents"], sold["gain_loss_cents"]), (0, 5))
        # 未出售的零值目标 2 月仍在册列示。
        self.assertEqual(device_row(report, "2026-02", "Z2")["closing_book_cents"], 0)

    def test_empty_assets_and_events_keep_all_periods(self):
        report = run(lineage_input([], [], as_of="2026-03"))
        self.assertEqual(report["events"], [])
        self.assertEqual([row["month"] for row in report["periods"]],
                         ["2026-01", "2026-02", "2026-03"])
        for row in report["periods"]:
            self.assertEqual(row["assets"], [])
            self.assertEqual(row["categories"], {})

    def test_cost_equal_salvage_depreciates_zero_but_stays_listed(self):
        asset = make_asset("S", cost=50, salvage=50, life=2)
        report = run(lineage_input([asset], [], as_of="2026-03"))
        for month in ("2026-01", "2026-02", "2026-03"):
            row = device_row(report, month, "S")
            self.assertEqual(row["depreciation_cents"], 0)
            self.assertEqual(row["closing_book_cents"], 50)


class BoundaryTests(unittest.TestCase):
    def test_range_boundary_120_months_accepted(self):
        report = run(lineage_input([], [], first="2026-01", as_of="2035-12"))
        self.assertEqual(len(report["periods"]), 120)

    def test_max_assets_and_chained_events_accepted(self):
        assets = [make_asset(f"a{i:03d}", cost=i, salvage=0, life=1) for i in range(200)]
        report = run(lineage_input(assets, [], as_of="2026-01"))
        self.assertEqual(len(period(report, "2026-01")["assets"]), 200)

        events = []
        source = "A"
        for i in range(1, 1001):
            target = f"T{i}"
            events.append(reconfigure(f"ev{i}", "2026-01", i, [source],
                                      [(target, "equipment", 1)]))
            source = target
        report = run(lineage_input([make_asset("A", cost=7, salvage=0, life=2)], events,
                                   as_of="2026-01"))
        self.assertEqual(len(report["events"]), 1000)
        final = device_row(report, "2026-01", "T1000")
        self.assertEqual((final["transfer_in_cents"], final["closing_book_cents"]), (7, 4))


class ValidationTests(unittest.TestCase):
    def _assert_rejected(self, data, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            run(data)

    def test_root_fields_and_months(self):
        data = lineage_input([], [])
        data["extra"] = 1
        self._assert_rejected(data, "invalid fields")
        self._assert_rejected(lineage_input([], [], first="2026-1"), "YYYY-MM")
        self._assert_rejected(lineage_input([], [], first="2026-02", as_of="2026-01"),
                              "report range")
        self._assert_rejected(lineage_input([], [], first="2026-01", as_of="2036-01"),
                              "report range")

    def test_asset_rules(self):
        self._assert_rejected(lineage_input([make_asset(start="2025-12")], []),
                              "outside report range")
        self._assert_rejected(lineage_input([make_asset(start="2027-01")], [],
                                            as_of="2026-04"), "outside report range")
        disposed = {**make_asset(), "disposal": {"month": "2026-02", "proceeds_cents": 1}}
        self._assert_rejected(lineage_input([disposed], []), "disposal must be null")
        self._assert_rejected(lineage_input([make_asset(cost=10, salvage=20)], []),
                              "salvage")
        self._assert_rejected(lineage_input([make_asset(life=0)], []), "life_months")
        self._assert_rejected(lineage_input([make_asset(cost=True)], []), "cost_cents")
        self._assert_rejected(lineage_input([make_asset(category="  ")], []), "category")
        self._assert_rejected(lineage_input([make_asset("a"), make_asset("a")], []),
                              "duplicate asset ID")
        self._assert_rejected(
            lineage_input([make_asset(f"a{i:03d}") for i in range(201)], []), "asset")

    def test_event_common_rules(self):
        self._assert_rejected(lineage_input([], [{"id": "e", "month": "2026-01",
                                                  "seq": 1, "kind": "lease"}]),
                              "unknown event kind")
        event = sell("e", "2026-01", 1, "a", 1)
        event["note"] = "x"
        self._assert_rejected(lineage_input([], [event]), "invalid fields")
        self._assert_rejected(lineage_input([], [sell("e", "2026-01", 1, "a", 1),
                                                 sell("e", "2026-01", 2, "a", 1)]),
                              "duplicate event ID")
        self._assert_rejected(lineage_input([], [sell("e1", "2026-01", 1, "a", 1),
                                                 sell("e2", "2026-01", 1, "b", 1)]),
                              "duplicate seq")
        # 同 seq 跨月允许。
        report = run(lineage_input([make_asset("a"), make_asset("b")],
                                   [sell("e1", "2026-01", 1, "a", 1),
                                    sell("e2", "2026-02", 1, "b", 1)]))
        self.assertEqual(len(report["events"]), 2)
        self._assert_rejected(lineage_input([], [sell("e", "2026-01", 0, "a", 1)]), "seq")
        self._assert_rejected(lineage_input([], [sell("e", "2026-01", 1000001, "a", 1)]),
                              "seq")
        self._assert_rejected(lineage_input([], [sell("e", "2026-05", 1, "a", 1)]),
                              "outside report range")
        self._assert_rejected(lineage_input([], [sell("e", "2026-01", 1, "a", True)]),
                              "proceeds_cents")
        self._assert_rejected(lineage_input([], [sell("e", "2026-01", 1, "a", 10**12 + 1)]),
                              "proceeds_cents")
        too_many = [sell(f"e{i}", "2026-01", i, "a", 1) for i in range(1, 1002)]
        self._assert_rejected(lineage_input([], too_many), "events")

    def test_reconfigure_rules(self):
        asset = make_asset("A")
        self._assert_rejected(lineage_input([asset], [reconfigure("e", "2026-01", 1, [], [("T", "c", 1)])]),
                              "sources")
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e", "2026-01", 1, ["A", "A"], [("T", "c", 1)])]),
            "duplicate source")
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e", "2026-01", 1, ["A"], [("T", "c", 2)])]),
            "single target requires weight 1")
        self._assert_rejected(lineage_input(
            [asset, make_asset("B")],
            [reconfigure("e", "2026-01", 1, ["A", "B"],
                         [("T1", "c", 1), ("T2", "c", 1)])]), "many-to-many")
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e", "2026-01", 1, ["A"], [("A", "c", 1)])]),
            "reuses a device ID")
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e1", "2026-01", 1, ["A"], [("T", "c", 1)]),
                      reconfigure("e2", "2026-01", 2, ["T"], [("T", "c", 1)])]),
            "reuses a device ID")
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e", "2026-01", 1, ["A"],
                                  [("T", "c", 1), ("T", "c", 1)])]), "reuses a device ID")
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e", "2026-01", 1, ["A"],
                                  [("T1", "c", 0), ("T2", "c", 1)])]), "weight")
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e", "2026-01", 1, ["A"],
                                  [("T1", "c", 10001), ("T2", "c", 1)])]), "weight")
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e", "2026-01", 1, ["A"], [("T", "  ", 1)])]),
            "category")
        nine = [f"s{i}" for i in range(9)]
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e", "2026-01", 1, nine, [("T", "c", 1)])]), "sources")
        nine_targets = [(f"T{i}", "c", 1) for i in range(9)]
        self._assert_rejected(lineage_input(
            [asset], [reconfigure("e", "2026-01", 1, ["A"], nine_targets)]), "targets")


class ReconcileTests(unittest.TestCase):
    def test_event_details_tie_to_monthly_rows_per_device(self):
        report = run(acceptance_input())
        reconcile(report["events"], report["periods"])  # 内置对账不抛错即通过
        per_device = {}
        for event in report["events"]:
            if event["kind"] == "reconfigure":
                for source in event["sources"]:
                    key = (event["month"], source["asset_id"])
                    per_device.setdefault(key, {})["out"] = \
                        per_device.get(key, {}).get("out", 0) + source["carrying_cents"]
                for target in event["targets"]:
                    key = (event["month"], target["asset_id"])
                    per_device.setdefault(key, {})["in"] = \
                        per_device.get(key, {}).get("in", 0) + target["carrying_cents"]
            else:
                key = (event["month"], event["asset"]["asset_id"])
                per_device.setdefault(key, {})["removed"] = \
                    per_device.get(key, {}).get("removed", 0) \
                    + event["asset"]["carrying_cents"]
        for row in report["periods"]:
            for asset in row["assets"]:
                got = per_device.get((row["month"], asset["asset_id"]), {})
                self.assertEqual(asset["transfer_in_cents"], got.get("in", 0))
                self.assertEqual(asset["transfer_out_cents"], got.get("out", 0))
                self.assertEqual(asset["carrying_removed_cents"], got.get("removed", 0))


class FixtureTests(unittest.TestCase):
    def test_fixture_matches_hand_written_expected_report(self):
        source = json.loads((ROOT / "fixtures" / "lineage_input.json").read_text())
        expected = json.loads((ROOT / "fixtures" / "lineage_expected.json").read_text())
        self.assertEqual(run(source), expected)

    def test_expected_report_is_internally_consistent(self):
        expected = json.loads((ROOT / "fixtures" / "lineage_expected.json").read_text())
        for row in expected["periods"]:
            categories = {}
            for asset in row["assets"]:
                self.assertEqual(
                    asset["closing_book_cents"],
                    asset["opening_book_cents"] + asset["acquired_cents"]
                    + asset["transfer_in_cents"] - asset["transfer_out_cents"]
                    - asset["depreciation_cents"] - asset["carrying_removed_cents"])
                slot = categories.setdefault(asset["category"], dict.fromkeys(METRICS, 0))
                for key in METRICS:
                    slot[key] += asset[key]
            self.assertEqual(row["categories"], categories)
            self.assertEqual(sum(a["transfer_in_cents"] for a in row["assets"]),
                             sum(a["transfer_out_cents"] for a in row["assets"]))
        for event in expected["events"]:
            if event["kind"] == "reconfigure":
                self.assertEqual(sum(s["carrying_cents"] for s in event["sources"]),
                                 sum(t["carrying_cents"] for t in event["targets"]))
                for side in (event["sources"], event["targets"]):
                    self.assertEqual([d["asset_id"] for d in side],
                                     sorted(d["asset_id"] for d in side))
            details = ([event["asset"]] if event["kind"] == "sell"
                       else event["sources"] + event["targets"])
            for detail in details:
                self.assertEqual(sum(detail["by_origin"].values()),
                                 detail["carrying_cents"])
                self.assertTrue(all(amount > 0 for amount in detail["by_origin"].values()))


class CliTests(unittest.TestCase):
    def run_cli(self, source, output):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run([sys.executable, "-m", "asset_lineage.cli",
                               "--source", str(source), "--output", str(output)],
                              cwd=ROOT, env=env, capture_output=True, text=True)

    def test_cli_success_atomic_output_and_existing_target_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            source.write_text(json.dumps(acceptance_input()))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(set(report), {"from_month", "as_of_month",
                                           "events", "periods"})
            stale = output.read_text()
            again = self.run_cli(source, output)
            self.assertEqual(again.returncode, 2)
            self.assertIn("asset-lineage", again.stderr)
            self.assertEqual(output.read_text(), stale)  # 旧目标保留
            self.assertEqual(list(Path(tmp).glob(".report-*")), [])  # 临时文件已清理

    def test_cli_invalid_input_exit_2_and_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            source.write_text(json.dumps(lineage_input([make_asset(cost=10, salvage=20)],
                                                       [])))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_cli_replay_late_reference_error_exit_2_and_no_partial_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            data = lineage_input([make_asset("A")],
                                 [reconfigure("ev1", "2026-02", 1, ["A"],
                                              [("T1", "equipment", 1)]),
                                  sell("ev2", "2026-03", 1, "A", 5)])
            source.write_text(json.dumps(data))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertIn("not on the books", result.stderr)
            self.assertFalse(output.exists())

    def test_cli_rejects_oversized_nan_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.json"
            big.write_text('{"from_month":"2026-01","as_of_month":"2026-02",'
                           '"assets":[],"events":' + " " * (8 * 1024 * 1024) + "[]}")
            self.assertEqual(self.run_cli(big, Path(tmp) / "o.json").returncode, 2)
            nan = Path(tmp) / "nan.json"
            nan.write_text('{"from_month":"2026-01","as_of_month":"2026-02",'
                           '"assets":[{"id":"a","category":"c","cost_cents":NaN,'
                           '"salvage_cents":0,"life_months":1,'
                           '"in_service_month":"2026-01","disposal":null}],"events":[]}')
            self.assertEqual(self.run_cli(nan, Path(tmp) / "o2.json").returncode, 2)
            dup = Path(tmp) / "dup.json"
            dup.write_text('{"from_month":"2026-01","from_month":"2026-01",'
                           '"as_of_month":"2026-02","assets":[],"events":[]}')
            self.assertEqual(self.run_cli(dup, Path(tmp) / "o3.json").returncode, 2)


if __name__ == "__main__":
    unittest.main()
