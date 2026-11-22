import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from asset_disclosure.engine import cell_bounds, optimize
from asset_disclosure.service import run

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

CATS = ["A", "B", "C"]
CUSTS = ["X", "Y", "Z"]


def make_asset(asset_id, category, cost, salvage, life, start="2026-01"):
    return {"id": asset_id, "category": category, "cost_cents": cost,
            "salvage_cents": salvage, "life_months": life,
            "in_service_month": start, "disposal": None}


def acceptance_lineage(robot_salvage=6):
    """验收台账：2 月合装 cell-a、出售 old-saw；junk 仅在 1 月月表出现。"""
    assets = [
        make_asset("lathe", "A", 4, 4, 1),
        make_asset("mill", "A", 2, 2, 1),
        make_asset("drill", "A", 8, 4, 2),
        make_asset("saw", "B", 8, 4, 2),
        make_asset("press", "B", 12, 6, 2),
        make_asset("cnc", "C", 8, 4, 2),
        make_asset("robot", "C", robot_salvage * 2, robot_salvage, 2),
        make_asset("old-saw", "B", 5, 1, 2),
        make_asset("junk", "C", 2, 0, 1),
    ]
    events = [
        {"id": "ev-sell-junk", "month": "2026-01", "seq": 1, "kind": "sell",
         "asset_id": "junk", "proceeds_cents": 1},
        {"id": "ev-merge-a", "month": "2026-02", "seq": 1, "kind": "reconfigure",
         "sources": ["lathe", "mill"],
         "targets": [{"id": "cell-a", "category": "A", "weight": 1}]},
        {"id": "ev-sell-old", "month": "2026-02", "seq": 2, "kind": "sell",
         "asset_id": "old-saw", "proceeds_cents": 3},
    ]
    return {"from_month": "2026-01", "as_of_month": "2026-02",
            "assets": assets, "events": events}


def acceptance_mapping():
    """恰好覆盖截止月 9 台设备（含零值的 lathe/mill/old-saw），不含 junk。"""
    return {"lathe": "X", "mill": "X", "cell-a": "X", "drill": "Y",
            "saw": "X", "press": "Y", "old-saw": "Y", "cnc": "X", "robot": "Z"}


def acceptance_cells(b_y="optional"):
    cells = [
        {"category": "A", "custodian": "X", "mode": "protected",
         "min_span_cents": 5},
        {"category": "A", "custodian": "Y", "mode": "optional",
         "suppression_cost": 1},
        {"category": "A", "custodian": "Z", "mode": "optional",
         "suppression_cost": 2},
        {"category": "B", "custodian": "X", "mode": "optional",
         "suppression_cost": 1},
        {"category": "B", "custodian": "Z", "mode": "structural_zero"},
        {"category": "C", "custodian": "X", "mode": "optional",
         "suppression_cost": 2},
        {"category": "C", "custodian": "Y", "mode": "structural_zero"},
        {"category": "C", "custodian": "Z", "mode": "optional",
         "suppression_cost": 2},
    ]
    by_key = {"B/Y": {"category": "B", "custodian": "Y", "mode": "optional",
                      "suppression_cost": 1}}
    if b_y == "public":
        by_key["B/Y"] = {"category": "B", "custodian": "Y", "mode": "public"}
    cells.insert(4, by_key["B/Y"])
    return cells


def disclosure_input(lineage, cells, mapping=None, categories=CATS,
                     custodians=CUSTS):
    return {"lineage": lineage, "categories": list(categories),
            "custodians": list(custodians),
            "custodian_by_device": (dict(mapping) if mapping is not None
                                    else acceptance_mapping()),
            "cells": cells}


def simple_lineage(device_values, as_of="2026-02"):
    """{设备: (类别, 期末值)} 的台账：成本等于残值，截止月在册且期末为给定值。"""
    assets = [make_asset(device, category, value, value, 1)
              for device, (category, value) in device_values.items()]
    return {"from_month": "2026-01", "as_of_month": as_of,
            "assets": assets, "events": []}


def table_input(truth, policies, as_of="2026-02"):
    """由 {行: {列: 真值}} 与 {键: 政策} 直接合成一份合法输入。"""
    categories = list(truth)
    custodians = list(next(iter(truth.values())))
    devices = {}
    mapping = {}
    counter = 0
    for category, row in truth.items():
        for custodian, value in row.items():
            device = f"d{counter}"
            counter += 1
            devices[device] = (category, value)
            mapping[device] = custodian
    cells = [{"category": category, "custodian": custodian,
              **policies[(category, custodian)]}
             for category in categories for custodian in custodians]
    return disclosure_input(simple_lineage(devices, as_of), cells,
                            mapping=mapping, categories=categories,
                            custodians=custodians)


def hidden_keys(report):
    return {(cell["category"], cell["custodian"])
            for cell in report["cells"] if cell["status"] == "hidden"}


EXPECTED_V1 = {
    "status": "ready",
    "as_of_month": "2026-02",
    "row_totals": [{"category": "A", "closing_book_cents": 10},
                   {"category": "B", "closing_book_cents": 10},
                   {"category": "C", "closing_book_cents": 10}],
    "column_totals": [{"custodian": "X", "closing_book_cents": 14},
                      {"custodian": "Y", "closing_book_cents": 10},
                      {"custodian": "Z", "closing_book_cents": 6}],
    "cells": [
        {"category": "A", "custodian": "X", "status": "hidden"},
        {"category": "A", "custodian": "Y", "status": "hidden"},
        {"category": "A", "custodian": "Z", "status": "shown",
         "closing_book_cents": 0},
        {"category": "B", "custodian": "X", "status": "hidden"},
        {"category": "B", "custodian": "Y", "status": "hidden"},
        {"category": "B", "custodian": "Z", "status": "shown",
         "closing_book_cents": 0},
        {"category": "C", "custodian": "X", "status": "shown",
         "closing_book_cents": 4},
        {"category": "C", "custodian": "Y", "status": "shown",
         "closing_book_cents": 0},
        {"category": "C", "custodian": "Z", "status": "shown",
         "closing_book_cents": 6},
    ],
}

EXPECTED_V2 = {
    "status": "ready",
    "as_of_month": "2026-02",
    "row_totals": EXPECTED_V1["row_totals"],
    "column_totals": EXPECTED_V1["column_totals"],
    "cells": [
        {"category": "A", "custodian": "X", "status": "hidden"},
        {"category": "A", "custodian": "Y", "status": "shown",
         "closing_book_cents": 4},
        {"category": "A", "custodian": "Z", "status": "hidden"},
        {"category": "B", "custodian": "X", "status": "shown",
         "closing_book_cents": 4},
        {"category": "B", "custodian": "Y", "status": "shown",
         "closing_book_cents": 6},
        {"category": "B", "custodian": "Z", "status": "shown",
         "closing_book_cents": 0},
        {"category": "C", "custodian": "X", "status": "hidden"},
        {"category": "C", "custodian": "Y", "status": "shown",
         "closing_book_cents": 0},
        {"category": "C", "custodian": "Z", "status": "hidden"},
    ],
}


class AcceptanceV1Tests(unittest.TestCase):
    """验收第一版：只有 A/X 敏感，额外遮蔽前三个各花 1 的格。"""

    def test_report_matches_hand_written_expected(self):
        report = run(disclosure_input(acceptance_lineage(), acceptance_cells()))
        self.assertEqual(report, EXPECTED_V1)

    def test_suppressed_set_and_reachable_range(self):
        report = run(disclosure_input(acceptance_lineage(), acceptance_cells()))
        self.assertEqual(hidden_keys(report),
                         {("A", "X"), ("A", "Y"), ("B", "X"), ("B", "Y")})
        truth = {("A", "X"): 6, ("A", "Y"): 4, ("A", "Z"): 0,
                 ("B", "X"): 4, ("B", "Y"): 6, ("B", "Z"): 0,
                 ("C", "X"): 4, ("C", "Y"): 0, ("C", "Z"): 6}
        self.assertEqual(cell_bounds(CATS, CUSTS, truth, hidden_keys(report),
                                     ("A", "X")), (0, 10))

    def test_span_boundary_at_exact_requirement(self):
        cells = acceptance_cells()
        cells[0]["min_span_cents"] = 10
        report = run(disclosure_input(acceptance_lineage(), cells))
        self.assertEqual(report["status"], "ready")
        cells[0]["min_span_cents"] = 11
        report = run(disclosure_input(acceptance_lineage(), cells))
        self.assertEqual(report, {"status": "blocked", "as_of_month": "2026-02"})


class AcceptanceV2Tests(unittest.TestCase):
    """验收第二版：B/Y 改为必须显示，最优改为遮蔽后三个各花 2 的格。"""

    def test_report_matches_hand_written_expected(self):
        report = run(disclosure_input(acceptance_lineage(),
                                      acceptance_cells(b_y="public")))
        self.assertEqual(report, EXPECTED_V2)

    def test_zero_truth_optional_cell_still_participates(self):
        report = run(disclosure_input(acceptance_lineage(),
                                      acceptance_cells(b_y="public")))
        # A/Z 真值为 0 仍被遮蔽参与反算；A/X 可达 0 至 6。
        self.assertEqual(hidden_keys(report),
                         {("A", "X"), ("A", "Z"), ("C", "X"), ("C", "Z")})
        truth = {("A", "X"): 6, ("A", "Y"): 4, ("A", "Z"): 0,
                 ("B", "X"): 4, ("B", "Y"): 6, ("B", "Z"): 0,
                 ("C", "X"): 4, ("C", "Y"): 0, ("C", "Z"): 6}
        self.assertEqual(cell_bounds(CATS, CUSTS, truth, hidden_keys(report),
                                     ("A", "X")), (0, 6))


class AcceptanceV3Tests(unittest.TestCase):
    """验收第三版：C/Z 真值降至 2，即使全部遮蔽 A/X 也只能在 4 至 6 间变化。"""

    def test_blocked_and_writes_only_status(self):
        data = disclosure_input(acceptance_lineage(robot_salvage=2),
                                acceptance_cells(b_y="public"))
        self.assertEqual(run(data), {"status": "blocked",
                                     "as_of_month": "2026-02"})

    def test_hidden_cycle_alone_is_not_enough(self):
        # 全部遮蔽后隐藏格仍成闭环，但整表可达跨度只有 2，必须拒发。
        truth = {("A", "X"): 6, ("A", "Y"): 4, ("A", "Z"): 0,
                 ("B", "X"): 4, ("B", "Y"): 6, ("B", "Z"): 0,
                 ("C", "X"): 4, ("C", "Y"): 0, ("C", "Z"): 2}
        policies = {("A", "X"): {"mode": "protected", "min_span_cents": 5},
                    ("A", "Y"): {"mode": "optional", "suppression_cost": 1},
                    ("A", "Z"): {"mode": "optional", "suppression_cost": 2},
                    ("B", "X"): {"mode": "optional", "suppression_cost": 1},
                    ("B", "Y"): {"mode": "public"},
                    ("B", "Z"): {"mode": "structural_zero"},
                    ("C", "X"): {"mode": "optional", "suppression_cost": 2},
                    ("C", "Y"): {"mode": "structural_zero"},
                    ("C", "Z"): {"mode": "optional", "suppression_cost": 2}}
        self.assertIsNone(optimize(CATS, CUSTS, truth, policies))
        full_hidden = {("A", "X"), ("A", "Y"), ("A", "Z"),
                       ("B", "X"), ("C", "X"), ("C", "Z")}
        self.assertEqual(cell_bounds(CATS, CUSTS, truth, full_hidden,
                                     ("A", "X")), (4, 6))


class SharedSuppressionTests(unittest.TestCase):
    """两个敏感格共享同一套遮蔽：成本按整表收一次，不逐格贪心。"""

    def setUp(self):
        truth = {"A": {"X": 4, "Y": 4, "Z": 4}, "B": {"X": 4, "Y": 4, "Z": 4}}
        policies = {("A", "X"): {"mode": "protected", "min_span_cents": 4},
                    ("A", "Y"): {"mode": "optional", "suppression_cost": 1},
                    ("A", "Z"): {"mode": "optional", "suppression_cost": 1},
                    ("B", "X"): {"mode": "optional", "suppression_cost": 1},
                    ("B", "Y"): {"mode": "protected", "min_span_cents": 4},
                    ("B", "Z"): {"mode": "optional", "suppression_cost": 1}}
        self.report = run(table_input(truth, policies))
        self.truth = {("A", "X"): 4, ("A", "Y"): 4, ("A", "Z"): 4,
                      ("B", "X"): 4, ("B", "Y"): 4, ("B", "Z"): 4}

    def test_one_shared_set_serves_both_protected_cells(self):
        # 逐格贪心会各花 2 再并集成更贵的集合；整表最优只花 2。
        self.assertEqual(hidden_keys(self.report),
                         {("A", "X"), ("A", "Y"), ("B", "X"), ("B", "Y")})
        hidden = hidden_keys(self.report)
        self.assertEqual(cell_bounds(["A", "B"], CUSTS, self.truth, hidden,
                                     ("A", "X")), (0, 8))
        self.assertEqual(cell_bounds(["A", "B"], CUSTS, self.truth, hidden,
                                     ("B", "Y")), (0, 8))

    def test_report_totals_and_shown_cells(self):
        self.assertEqual(self.report["row_totals"],
                         [{"category": "A", "closing_book_cents": 12},
                          {"category": "B", "closing_book_cents": 12}])
        self.assertEqual(self.report["column_totals"],
                         [{"custodian": "X", "closing_book_cents": 8},
                          {"custodian": "Y", "closing_book_cents": 8},
                          {"custodian": "Z", "closing_book_cents": 8}])
        shown = {cell["category"] + cell["custodian"]:
                 cell["closing_book_cents"]
                 for cell in self.report["cells"] if cell["status"] == "shown"}
        self.assertEqual(shown, {"AZ": 4, "BZ": 4})


class TieBreakTests(unittest.TestCase):
    """同损失时的两级决胜：先比遮蔽格数，再比升序键列表的字典序。"""

    TRUTH = {"A": {"X": 4, "Y": 4, "Z": 4},
             "B": {"X": 4, "Y": 4, "Z": 4},
             "C": {"X": 4, "Y": 4, "Z": 4}}

    def _policies(self, costs):
        policies = {("A", "X"): {"mode": "protected", "min_span_cents": 4}}
        for key, cost in costs.items():
            policies[key] = {"mode": "optional", "suppression_cost": cost}
        return policies

    def test_equal_cost_prefers_fewer_suppressed_cells(self):
        # {A/Y,B/X,B/Y} 与 {A/Y,B/X,B/Z,C/Y,C/Z} 同花 6，前者格数更少。
        costs = {("A", "Y"): 1, ("A", "Z"): 10,
                 ("B", "X"): 1, ("B", "Y"): 4, ("B", "Z"): 1,
                 ("C", "X"): 10, ("C", "Y"): 1, ("C", "Z"): 2}
        report = run(table_input(self.TRUTH, self._policies(costs)))
        self.assertEqual(hidden_keys(report),
                         {("A", "X"), ("A", "Y"), ("B", "X"), ("B", "Y")})

    def test_equal_cost_and_count_prefers_lexicographically_smaller_keys(self):
        # {A/Y,B/X,B/Y} 与 {A/Z,C/X,C/Z} 同花 3 且同为 3 格，取键列表较小者。
        costs = {("A", "Y"): 1, ("A", "Z"): 1,
                 ("B", "X"): 1, ("B", "Y"): 1, ("B", "Z"): 5,
                 ("C", "X"): 1, ("C", "Y"): 5, ("C", "Z"): 1}
        report = run(table_input(self.TRUTH, self._policies(costs)))
        self.assertEqual(hidden_keys(report),
                         {("A", "X"), ("A", "Y"), ("B", "X"), ("B", "Y")})


class LineageDrivenTests(unittest.TestCase):
    """实际拆并与出售改变截止月归属和小计。"""

    def test_reconfigure_and_sale_move_subtotals(self):
        lineage = {
            "from_month": "2026-01", "as_of_month": "2026-02",
            "assets": [make_asset("old-1", "legacy", 10, 10, 1),
                       make_asset("old-2", "legacy", 6, 6, 1),
                       make_asset("keep-1", "modern", 4, 4, 1),
                       make_asset("sold-1", "legacy", 5, 5, 1),
                       make_asset("gone", "legacy", 3, 3, 1)],
            "events": [
                {"id": "ev-sell-gone", "month": "2026-01", "seq": 1,
                 "kind": "sell", "asset_id": "gone", "proceeds_cents": 3},
                {"id": "ev-move", "month": "2026-02", "seq": 1,
                 "kind": "reconfigure", "sources": ["old-1"],
                 "targets": [{"id": "new-1", "category": "modern",
                              "weight": 1}]},
                {"id": "ev-sell", "month": "2026-02", "seq": 2,
                 "kind": "sell", "asset_id": "sold-1", "proceeds_cents": 5},
            ],
        }
        cells = [
            {"category": "legacy", "custodian": "X", "mode": "optional",
             "suppression_cost": 1},
            {"category": "legacy", "custodian": "Y", "mode": "optional",
             "suppression_cost": 1},
            {"category": "modern", "custodian": "X", "mode": "protected",
             "min_span_cents": 4},
            {"category": "modern", "custodian": "Y", "mode": "optional",
             "suppression_cost": 5},
        ]
        mapping = {"old-1": "X", "new-1": "X", "old-2": "X",
                   "keep-1": "Y", "sold-1": "Y"}
        report = run(disclosure_input(lineage, cells, mapping=mapping,
                                      categories=["legacy", "modern"],
                                      custodians=["X", "Y"]))
        # 改造把 old-1 的 10 分迁入 modern 行（新设备 new-1），出售转出的
        # 5 分不计入期末；gone 只在 1 月月表，不在归口映射内。
        self.assertEqual(report["row_totals"],
                         [{"category": "legacy", "closing_book_cents": 6},
                          {"category": "modern", "closing_book_cents": 14}])
        self.assertEqual(report["column_totals"],
                         [{"custodian": "X", "closing_book_cents": 16},
                          {"custodian": "Y", "closing_book_cents": 4}])
        self.assertEqual(hidden_keys(report),
                         {("legacy", "X"), ("legacy", "Y"),
                          ("modern", "X"), ("modern", "Y")})

    def test_empty_as_of_month_table_blocks_with_empty_mapping(self):
        lineage = {"from_month": "2026-01", "as_of_month": "2026-02",
                   "assets": [], "events": []}
        cells = [{"category": "A", "custodian": "X", "mode": "protected",
                  "min_span_cents": 1}]
        report = run(disclosure_input(lineage, cells, mapping={},
                                      categories=["A"], custodians=["X"]))
        self.assertEqual(report, {"status": "blocked",
                                  "as_of_month": "2026-02"})


class ReorderTests(unittest.TestCase):
    def test_input_permutation_keeps_report(self):
        base = run(disclosure_input(acceptance_lineage(), acceptance_cells()))
        lineage = acceptance_lineage()
        shuffled_lineage = {"from_month": "2026-01", "as_of_month": "2026-02",
                            "assets": list(reversed(lineage["assets"])),
                            "events": list(reversed(lineage["events"]))}
        shuffled_mapping = dict(reversed(list(acceptance_mapping().items())))
        shuffled = disclosure_input(shuffled_lineage,
                                    list(reversed(acceptance_cells())),
                                    mapping=shuffled_mapping,
                                    categories=["C", "B", "A"],
                                    custodians=["Z", "Y", "X"])
        self.assertEqual(base, run(shuffled))


class ValidationTests(unittest.TestCase):
    def _assert_rejected(self, data, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            run(data)

    def _base(self):
        return disclosure_input(acceptance_lineage(), acceptance_cells())

    def test_root_fields(self):
        data = self._base()
        data["extra"] = 1
        self._assert_rejected(data, "invalid fields")
        del data["extra"]
        del data["cells"]
        self._assert_rejected(data, "invalid fields")
        data = self._base()
        data["lineage"] = []
        self._assert_rejected(data, "lineage must be an object")

    def test_invalid_lineage_rejected(self):
        data = self._base()
        data["lineage"]["assets"][0]["salvage_cents"] = 99
        self._assert_rejected(data, "salvage")
        data = self._base()
        data["lineage"]["events"].append(
            {"id": "ev-bad", "month": "2026-02", "seq": 3, "kind": "sell",
             "asset_id": "junk", "proceeds_cents": 1})
        self._assert_rejected(data, "not on the books")
        data = self._base()
        data["lineage"]["assets"][0]["cost_cents"] = True
        self._assert_rejected(data, "cost_cents")

    def test_label_rules(self):
        for field in ("categories", "custodians"):
            data = self._base()
            data[field] = []
            self._assert_rejected(data, field)
            data = self._base()
            data[field] = ["A", "B", "C", "D", "E"]
            self._assert_rejected(data, field)
            data = self._base()
            data[field] = ["A", "A", "B"]
            self._assert_rejected(data, "duplicate")
            data = self._base()
            data[field] = ["A", "  ", "B"]
            self._assert_rejected(data, "nonempty")
            data = self._base()
            data[field] = ["A", "x" * 101, "B"]
            self._assert_rejected(data, "nonempty")
            data = self._base()
            data[field] = ["A", 1, "B"]
            self._assert_rejected(data, "nonempty")

    def test_mapping_rules(self):
        data = self._base()
        data["custodian_by_device"] = []
        self._assert_rejected(data, "must be an object")
        data = self._base()
        del data["custodian_by_device"]["old-saw"]
        self._assert_rejected(data, "must equal the as-of month device IDs")
        data = self._base()
        data["custodian_by_device"]["junk"] = "X"  # 仅在以往月份列出的设备
        self._assert_rejected(data, "must equal the as-of month device IDs")
        data = self._base()
        data["custodian_by_device"]["cell-a"] = "warehouse"
        self._assert_rejected(data, "not listed in custodians")
        data = self._base()
        data["custodian_by_device"]["cell-a"] = 7
        self._assert_rejected(data, "not listed in custodians")

    def test_category_coverage(self):
        data = self._base()
        data["categories"] = ["A", "B"]
        data["cells"] = [cell for cell in data["cells"]
                         if cell["category"] != "C"]
        self._assert_rejected(data, "as-of month category not listed")

    def test_cell_structure_rules(self):
        data = self._base()
        data["cells"] = data["cells"][:-1]
        self._assert_rejected(data, "cells")
        data = self._base()
        data["cells"].append(dict(data["cells"][0]))
        self._assert_rejected(data, "cells")
        data = self._base()
        data["cells"][1] = dict(data["cells"][0])
        self._assert_rejected(data, "duplicate cell policy")
        data = self._base()
        data["cells"][0]["category"] = "D"
        self._assert_rejected(data, "not listed in categories")
        data = self._base()
        data["cells"][0]["custodian"] = "W"
        self._assert_rejected(data, "not listed in custodians")
        data = self._base()
        data["cells"][0]["mode"] = "secret"
        self._assert_rejected(data, "unknown cell mode")
        data = self._base()
        data["cells"][0]["note"] = "x"
        self._assert_rejected(data, "invalid fields")
        data = self._base()
        del data["cells"][0]["min_span_cents"]
        self._assert_rejected(data, "invalid fields")
        data = self._base()
        data["cells"][1]["min_span_cents"] = 1
        self._assert_rejected(data, "invalid fields")
        data = self._base()
        data["cells"][5]["suppression_cost"] = 1  # structural_zero 不带金额字段
        self._assert_rejected(data, "invalid fields")

    def test_cell_value_rules(self):
        data = self._base()
        data["cells"][0]["min_span_cents"] = 0
        self._assert_rejected(data, "min_span_cents")
        data = self._base()
        data["cells"][0]["min_span_cents"] = 10**12 + 1
        self._assert_rejected(data, "min_span_cents")
        data = self._base()
        data["cells"][0]["min_span_cents"] = True
        self._assert_rejected(data, "min_span_cents")
        data = self._base()
        data["cells"][1]["suppression_cost"] = 0
        self._assert_rejected(data, "suppression_cost")
        data = self._base()
        data["cells"][1]["suppression_cost"] = 10**6 + 1
        self._assert_rejected(data, "suppression_cost")
        data = self._base()
        data["cells"][1]["suppression_cost"] = False
        self._assert_rejected(data, "suppression_cost")

    def test_at_least_one_protected(self):
        data = self._base()
        for cell in data["cells"]:
            if cell["mode"] == "protected":
                cell.clear()
                cell.update({"category": "A", "custodian": "X",
                             "mode": "public"})
        self._assert_rejected(data, "at least one protected")

    def test_structural_zero_must_match_truth(self):
        data = self._base()
        for cell in data["cells"]:
            if (cell["category"], cell["custodian"]) == ("C", "Z"):
                cell.clear()
                cell.update({"category": "C", "custodian": "Z",
                             "mode": "structural_zero"})
        self._assert_rejected(data, "structural_zero cell carries a balance")


class FixtureTests(unittest.TestCase):
    def test_fixture_matches_hand_written_expected(self):
        source = json.loads((ROOT / "fixtures" / "disclosure_input.json").read_text())
        self.assertEqual(run(source), EXPECTED_V1)

    def test_fixture_matches_expected_file(self):
        source = json.loads((ROOT / "fixtures" / "disclosure_input.json").read_text())
        expected = json.loads((ROOT / "fixtures" / "disclosure_expected.json").read_text())
        self.assertEqual(run(source), expected)


class CliTests(unittest.TestCase):
    def run_cli(self, source, output):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run([sys.executable, "-m", "asset_disclosure.cli",
                               "--source", str(source), "--output", str(output)],
                              cwd=ROOT, env=env, capture_output=True, text=True)

    def test_cli_ready_atomic_output_and_stdout_discipline(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            source.write_text(json.dumps(disclosure_input(
                acceptance_lineage(), acceptance_cells())))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()), EXPECTED_V1)
            brief = json.loads(result.stdout)
            self.assertEqual(brief, {"as_of_month": "2026-02",
                                     "status": "ready"})
            self.assertNotIn("closing_book_cents", result.stdout)
            stale = output.read_text()
            again = self.run_cli(source, output)
            self.assertEqual(again.returncode, 2)
            self.assertIn("asset-balance-disclosure", again.stderr)
            self.assertEqual(output.read_text(), stale)  # 旧目标保留
            self.assertEqual(list(Path(tmp).glob(".report-*")), [])

    def test_cli_blocked_exit_0_and_minimal_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            source.write_text(json.dumps(disclosure_input(
                acceptance_lineage(robot_salvage=2),
                acceptance_cells(b_y="public"))))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()),
                             {"status": "blocked", "as_of_month": "2026-02"})
            self.assertEqual(json.loads(result.stdout),
                             {"as_of_month": "2026-02", "status": "blocked"})

    def test_cli_invalid_input_exit_2_and_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            data = disclosure_input(acceptance_lineage(), acceptance_cells())
            data["custodian_by_device"]["junk"] = "X"
            source.write_text(json.dumps(data))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_cli_rejects_oversized_nan_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.json"
            big.write_text('{"lineage":{},' + " " * (8 * 1024 * 1024)
                           + '"categories":[],"custodians":[],'
                             '"custodian_by_device":{},"cells":[]}')
            self.assertEqual(self.run_cli(big, Path(tmp) / "o.json").returncode, 2)
            nan = Path(tmp) / "nan.json"
            nan.write_text('{"lineage":{"from_month":"2026-01",'
                           '"as_of_month":"2026-01","assets":[{"id":"a",'
                           '"category":"A","cost_cents":NaN,"salvage_cents":0,'
                           '"life_months":1,"in_service_month":"2026-01",'
                           '"disposal":null}],"events":[]},'
                           '"categories":["A"],"custodians":["X"],'
                           '"custodian_by_device":{"a":"X"},'
                           '"cells":[{"category":"A","custodian":"X",'
                           '"mode":"protected","min_span_cents":1}]}')
            self.assertEqual(self.run_cli(nan, Path(tmp) / "o2.json").returncode, 2)
            dup = Path(tmp) / "dup.json"
            dup.write_text('{"lineage":{},"lineage":{},'
                           '"categories":[],"custodians":[],'
                           '"custodian_by_device":{},"cells":[]}')
            self.assertEqual(self.run_cli(dup, Path(tmp) / "o3.json").returncode, 2)


if __name__ == "__main__":
    unittest.main()
