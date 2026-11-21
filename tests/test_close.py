import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from asset_close.engine import build_journal, build_periods, reconcile
from asset_close.model import validate
from asset_close.service import run

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def make_asset(asset_id="dev", category="equipment", cost=100, salvage=0, life=3,
               start="2026-01", disposal=None):
    return {"id": asset_id, "category": category, "cost_cents": cost,
            "salvage_cents": salvage, "life_months": life,
            "in_service_month": start, "disposal": disposal}


def revision(known, asset):
    return {"known_month": known, "asset": asset}


def close_input(revisions, first="2026-01", as_of="2026-04"):
    return {"first_close_month": first, "as_of_month": as_of, "revisions": revisions}


def row_keys(rows):
    return [(r["posted_month"], r["economic_month"], r["asset_id"],
             r["category"], r["kind"]) for r in rows]


def amounts(rows, posted=None, economic=None, kind=None):
    return [r["amount_cents"] for r in rows
            if (posted is None or r["posted_month"] == posted)
            and (economic is None or r["economic_month"] == economic)
            and (kind is None or r["kind"] == kind)]


class AcceptanceExampleTests(unittest.TestCase):
    """任务书给出的完整业务例子：1-4 月逐步更正，逐笔手工预期值。"""

    def setUp(self):
        self.data = close_input([
            revision("2026-01", make_asset(cost=100, disposal=None)),
            revision("2026-03", make_asset(cost=101,
                                           disposal={"month": "2026-03", "proceeds_cents": 80})),
            revision("2026-04", make_asset(cost=101,
                                           disposal={"month": "2026-02", "proceeds_cents": 60})),
        ])
        self.report = run(self.data)

    def test_journal_matches_hand_expected_rows(self):
        expected = [
            ("2026-01", "2026-01", "dev", "equipment", "cost", 100),
            ("2026-01", "2026-01", "dev", "equipment", "depreciation", 33),
            ("2026-02", "2026-02", "dev", "equipment", "depreciation", 33),
            ("2026-03", "2026-01", "dev", "equipment", "cost", 1),
            ("2026-03", "2026-02", "dev", "equipment", "depreciation", 1),
            ("2026-03", "2026-03", "dev", "equipment", "carrying_removed", 34),
            ("2026-03", "2026-03", "dev", "equipment", "proceeds", 80),
            ("2026-04", "2026-02", "dev", "equipment", "carrying_removed", 68),
            ("2026-04", "2026-02", "dev", "equipment", "depreciation", -34),
            ("2026-04", "2026-02", "dev", "equipment", "proceeds", 60),
            ("2026-04", "2026-03", "dev", "equipment", "carrying_removed", -34),
            ("2026-04", "2026-03", "dev", "equipment", "proceeds", -80),
        ]
        actual = [(r["posted_month"], r["economic_month"], r["asset_id"],
                   r["category"], r["kind"], r["amount_cents"]) for r in self.report["journal"]]
        self.assertEqual(actual, expected)
        self.assertEqual(row_keys(self.report["journal"]), sorted(row_keys(self.report["journal"])))

    def test_booked_periods_keep_historical_figures(self):
        by_month = {p["month"]: p["booked"]["equipment"] for p in self.report["periods"]}
        self.assertEqual(by_month["2026-01"]["closing_book_cents"], 67)
        self.assertEqual(by_month["2026-02"]["closing_book_cents"], 34)
        self.assertEqual(by_month["2026-03"]["gain_loss_cents"], 46)
        self.assertEqual(by_month["2026-03"]["closing_book_cents"], 0)
        # 3 月没有经济月为 3 月的折旧。
        self.assertEqual(amounts(self.report["journal"], posted="2026-03",
                                 economic="2026-03", kind="depreciation"), [])
        self.assertEqual(by_month["2026-04"]["gain_loss_cents"], -54)
        self.assertEqual(by_month["2026-04"]["depreciation_cents"], -34)
        self.assertEqual(by_month["2026-04"]["carrying_removed_cents"], 34)
        self.assertEqual(by_month["2026-04"]["proceeds_cents"], -20)

    def test_restated_uses_final_versions_by_economic_month(self):
        by_month = {p["month"]: p["restated"].get("equipment") for p in self.report["periods"]}
        self.assertEqual(by_month["2026-01"], {
            "cost_cents": 101, "depreciation_cents": 33, "carrying_removed_cents": 0,
            "proceeds_cents": 0, "gain_loss_cents": 0, "closing_book_cents": 68})
        self.assertEqual(by_month["2026-02"]["gain_loss_cents"], -8)
        self.assertEqual(by_month["2026-02"]["carrying_removed_cents"], 68)
        self.assertEqual(by_month["2026-02"]["proceeds_cents"], 60)
        self.assertEqual(by_month["2026-02"]["depreciation_cents"], 0)
        self.assertEqual(by_month["2026-02"]["closing_book_cents"], 0)
        self.assertIsNone(by_month["2026-03"])
        self.assertIsNone(by_month["2026-04"])

    def test_no_zero_net_rows_and_one_row_per_key_per_close_month(self):
        for row in self.report["journal"]:
            self.assertNotEqual(row["amount_cents"], 0)
        seen = set()
        for row in self.report["journal"]:
            key = (row["posted_month"], row["economic_month"], row["asset_id"],
                   row["category"], row["kind"])
            self.assertNotIn(key, seen)
            seen.add(key)


class PrefixInvarianceTests(unittest.TestCase):
    def test_same_known_month_prefix_reproduces_identical_older_closes(self):
        revisions = [
            revision("2026-01", make_asset("a", cost=100)),
            revision("2026-02", make_asset("a", cost=105)),
            revision("2026-05", make_asset("a", cost=108)),
            revision("2026-03", make_asset("b", category="furniture", cost=200, life=6)),
        ]
        full = run(close_input(revisions, as_of="2026-06"))
        prefix = run(close_input(
            [r for r in revisions if r["known_month"] <= "2026-03"], as_of="2026-03"))
        for cutoff in ("2026-01", "2026-02", "2026-03"):
            full_rows = [r for r in full["journal"] if r["posted_month"] <= cutoff]
            prefix_rows = [r for r in prefix["journal"] if r["posted_month"] <= cutoff]
            self.assertEqual(full_rows, prefix_rows)
        # 未来才知道的版本不能改变早先关账月的任何流水行：2 月报告不含 2 月后修订。
        feb_only = run(close_input([r for r in revisions if r["known_month"] <= "2026-02"],
                                   as_of="2026-02"))
        self.assertEqual(
            [r for r in full["journal"] if r["posted_month"] <= "2026-02"],
            feb_only["journal"])

    def test_input_order_does_not_change_result(self):
        revisions = [
            revision("2026-03", make_asset(cost=101,
                                           disposal={"month": "2026-03", "proceeds_cents": 80})),
            revision("2026-01", make_asset(cost=100)),
        ]
        one = run(close_input(revisions))
        two = run(close_input(list(reversed(revisions))))
        self.assertEqual(one, two)


class ReconciliationTests(unittest.TestCase):
    def test_cumulative_flows_and_closing_book_tie_by_category(self):
        data = close_input([
            revision("2026-01", make_asset("a", "equipment", cost=100, life=3)),
            revision("2026-02", make_asset("a", "tools", cost=100, life=3)),
            revision("2026-03", make_asset("b", "equipment", cost=200, salvage=50, life=4,
                                           disposal={"month": "2026-03", "proceeds_cents": 120})),
        ], as_of="2026-05")
        report = run(data)
        rows, restated = build_journal(*validate(data))[:2]
        reconcile(rows, restated, validate(data)[1])  # 内置对账不抛错即通过

        booked_totals = self._cumulative(report, "booked")
        restated_totals = self._cumulative(report, "restated")
        self.assertEqual(booked_totals, restated_totals)

    @staticmethod
    def _cumulative(report, basis):
        totals = {}
        for period in report["periods"]:
            for category, values in period[basis].items():
                slot = totals.setdefault(category, {})
                for key in ("cost_cents", "depreciation_cents", "carrying_removed_cents",
                            "proceeds_cents"):
                    slot[key] = slot.get(key, 0) + values[key]
                slot["closing_book_cents"] = values["closing_book_cents"]
        return totals


class CategoryAndFutureDisposalTests(unittest.TestCase):
    def test_category_change_keeps_two_sided_entries_even_with_same_amounts(self):
        data = close_input([
            revision("2026-01", make_asset(category="old")),
            revision("2026-02", make_asset(category="new")),
        ], as_of="2026-02")
        rows = run(data)["journal"]
        feb = [r for r in rows if r["posted_month"] == "2026-02"]
        self.assertIn(("old", "cost", -100), [(r["category"], r["kind"], r["amount_cents"]) for r in feb])
        self.assertIn(("new", "cost", 100), [(r["category"], r["kind"], r["amount_cents"]) for r in feb])
        self.assertIn(("old", "depreciation", -33),
                      [(r["category"], r["kind"], r["amount_cents"]) for r in feb])
        self.assertIn(("new", "depreciation", 33),
                      [(r["category"], r["kind"], r["amount_cents"]) for r in feb])

    def test_early_known_future_disposal_revoked_before_occurrence_leaves_no_trace(self):
        data = close_input([
            revision("2026-01", make_asset(disposal={"month": "2026-03", "proceeds_cents": 80})),
            revision("2026-02", make_asset(disposal=None)),
        ], as_of="2026-03")
        rows = run(data)["journal"]
        self.assertTrue(all(r["kind"] not in ("proceeds", "carrying_removed") for r in rows))
        # 折旧照常：33、33、34。
        self.assertEqual(amounts(rows, kind="depreciation"), [33, 33, 34])

    def test_future_disposal_known_today_does_not_post_before_its_month(self):
        data = close_input([
            revision("2026-01", make_asset(disposal={"month": "2026-03", "proceeds_cents": 80})),
        ], as_of="2026-02")
        rows = run(data)["journal"]
        self.assertEqual(amounts(rows, kind="proceeds"), [])
        self.assertEqual(amounts(rows, kind="carrying_removed"), [])
        self.assertEqual(amounts(rows, kind="depreciation"), [33, 33])

    def test_disposal_then_reinstate_restores_depreciation(self):
        data = close_input([
            revision("2026-01", make_asset(disposal={"month": "2026-02", "proceeds_cents": 60})),
            revision("2026-03", make_asset(disposal=None)),
        ], as_of="2026-03")
        rows = run(data)["journal"]
        mar = [r for r in rows if r["posted_month"] == "2026-03"]
        # 冲回 2 月转出与所得，并补提 2 月折旧 33、确认 3 月折旧 34。
        self.assertIn(("2026-02", "carrying_removed", -67),
                      [(r["economic_month"], r["kind"], r["amount_cents"]) for r in mar])
        self.assertIn(("2026-02", "proceeds", -60),
                      [(r["economic_month"], r["kind"], r["amount_cents"]) for r in mar])
        self.assertIn(("2026-02", "depreciation", 33),
                      [(r["economic_month"], r["kind"], r["amount_cents"]) for r in mar])
        self.assertIn(("2026-03", "depreciation", 34),
                      [(r["economic_month"], r["kind"], r["amount_cents"]) for r in mar])


class EmptyAndBoundaryTests(unittest.TestCase):
    def test_empty_revisions_emit_all_periods_with_empty_bases(self):
        report = run(close_input([], as_of="2026-03"))
        self.assertEqual(report["journal"], [])
        self.assertEqual([p["month"] for p in report["periods"]],
                         ["2026-01", "2026-02", "2026-03"])
        self.assertTrue(all(p["booked"] == {} and p["restated"] == {} for p in report["periods"]))

    def test_months_without_activity_still_listed_and_carry_closing_book(self):
        data = close_input([
            revision("2026-01", make_asset(cost=100, life=6)),
        ], as_of="2026-04")
        report = run(data)
        periods = {p["month"]: p for p in report["periods"]}
        # 3、4 月没有新修订也没有到期键之外的事件；booked 仍保留累计期末账面。
        # 寿命 6 个月、成本 100：累计折旧 floor(100i/6) = 16、33、50、66、83、100。
        self.assertEqual(periods["2026-03"]["booked"]["equipment"]["closing_book_cents"], 50)
        self.assertEqual(periods["2026-04"]["booked"]["equipment"]["closing_book_cents"], 34)
        self.assertEqual(periods["2026-04"]["restated"]["equipment"]["closing_book_cents"], 34)

    def test_disposal_in_service_month_posts_cost_and_removal_together(self):
        data = close_input([
            revision("2026-01", make_asset(cost=100,
                                           disposal={"month": "2026-01", "proceeds_cents": 80})),
        ], as_of="2026-02")
        rows = run(data)["journal"]
        self.assertEqual(amounts(rows, kind="depreciation"), [])
        self.assertEqual(amounts(rows, economic="2026-01", kind="cost"), [100])
        self.assertEqual(amounts(rows, economic="2026-01", kind="carrying_removed"), [100])
        self.assertEqual(amounts(rows, economic="2026-01", kind="proceeds"), [80])
        period = run(data)["periods"][0]
        self.assertEqual(period["booked"]["equipment"]["gain_loss_cents"], -20)
        self.assertEqual(period["booked"]["equipment"]["closing_book_cents"], 0)
        # 处置后月份类别整体为零而省略。
        self.assertEqual(run(data)["periods"][1]["booked"], {})

    def test_integer_cent_allocation_matches_v1_floor_schedule(self):
        data = close_input([
            revision("2026-01", make_asset(cost=100, salvage=1, life=3)),
        ], as_of="2026-03")
        rows = run(data)["journal"]
        # floor 分配：33、33、33，期满保留残值 1。
        self.assertEqual(amounts(rows, kind="depreciation"), [33, 33, 33])
        last = run(data)["periods"][-1]
        self.assertEqual(last["restated"]["equipment"]["closing_book_cents"], 1)

    def test_zero_depreciable_base_keeps_salvage_book_value(self):
        data = close_input([
            revision("2026-01", make_asset(cost=50, salvage=50, life=2)),
        ], as_of="2026-02")
        report = run(data)
        self.assertEqual(amounts(report["journal"], kind="depreciation"), [])
        self.assertEqual(report["periods"][-1]["restated"]["equipment"]["closing_book_cents"], 50)

    def test_range_boundary_120_months_accepted(self):
        data = close_input([], first="2026-01", as_of="2035-12")
        report = run(data)
        self.assertEqual(len(report["periods"]), 120)

    def test_post_cutoff_revision_validates_but_has_no_effect(self):
        base = [revision("2026-01", make_asset(cost=100))]
        before = run(close_input(base, as_of="2026-02"))
        after = run(close_input(base + [
            revision("2026-07", make_asset(cost=999,
                                           disposal={"month": "2026-02", "proceeds_cents": 1})),
        ], as_of="2026-02"))
        self.assertEqual(before, after)

    def test_asset_in_service_in_future_posts_nothing_before_that_month(self):
        data = close_input([
            revision("2026-01", make_asset(cost=120, start="2026-03")),
        ], as_of="2026-04")
        report = run(data)
        self.assertEqual([r["posted_month"] for r in report["journal"]],
                         ["2026-03", "2026-03", "2026-04"])
        self.assertEqual(report["periods"][0]["booked"], {})
        self.assertEqual(report["periods"][0]["restated"], {})
        self.assertEqual(report["periods"][1]["restated"], {})
        self.assertEqual(report["periods"][2]["restated"]["equipment"]["closing_book_cents"], 80)

    def test_in_service_month_migration_reverses_old_key_and_books_new_key(self):
        data = close_input([
            revision("2026-01", make_asset(cost=120, start="2026-02")),
            revision("2026-03", make_asset(cost=120, start="2026-01")),
        ], as_of="2026-03")
        mar = [(r["economic_month"], r["kind"], r["amount_cents"])
               for r in run(data)["journal"] if r["posted_month"] == "2026-03"]
        self.assertIn(("2026-02", "cost", -120), mar)
        self.assertIn(("2026-01", "cost", 120), mar)
        self.assertIn(("2026-01", "depreciation", 40), mar)
        # 旧 2 月折旧键本就未发布过（2 月关账时按 2 月投入只提了 2 月当月折旧），
        # 迁移后 2 月折旧的应有额仍为 40（成为第二个使用月），无需冲销。
        self.assertEqual(sum(amt for eco, kind, amt in mar if kind == "depreciation"), 80)


class ValidationTests(unittest.TestCase):
    def _assert_rejected(self, data, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            run(data)

    def test_duplicate_revision_same_known_month_rejected(self):
        self._assert_rejected(close_input([
            revision("2026-01", make_asset(cost=100)),
            revision("2026-01", make_asset(cost=101)),
        ]), "duplicate revision")

    def test_known_month_before_first_close_rejected(self):
        self._assert_rejected(close_input([
            revision("2025-12", make_asset()),
        ]), "known_month")

    def test_in_service_before_first_close_rejected(self):
        self._assert_rejected(close_input([
            revision("2026-01", make_asset(start="2025-12")),
        ]), "in_service_month")

    def test_range_outside_1_to_120_months_rejected(self):
        self._assert_rejected(close_input([], first="2026-01", as_of="2036-01"),
                              "close range")

    def test_too_many_distinct_assets_rejected(self):
        revisions = [revision("2026-01", make_asset(f"a{i:03d}")) for i in range(201)]
        self._assert_rejected(close_input(revisions), "distinct asset")

    def test_too_many_revisions_rejected(self):
        revisions = [revision("2026-01", make_asset(f"a{i:03d}")) for i in range(200)]
        revisions += [revision("2026-01", make_asset("a000", cost=1 + i)) for i in range(2201)]
        self.assertEqual(len(revisions), 2401)
        self._assert_rejected(close_input(revisions), "revisions")

    def test_v1_asset_rules_still_enforced(self):
        self._assert_rejected(close_input([
            revision("2026-01", make_asset(cost=10, salvage=20))]), "salvage")
        self._assert_rejected(close_input([
            revision("2026-01", make_asset(disposal={"month": "2025-12",
                                                     "proceeds_cents": 1}))]), "service")
        self._assert_rejected(close_input([
            revision("2026-01", make_asset(life=0))]), "life_months")
        self._assert_rejected(close_input([
            revision("2026-01", make_asset(category="   "))]), "category")

    def test_unknown_fields_and_bad_types_rejected(self):
        data = close_input([revision("2026-01", make_asset())])
        data["extra"] = 1
        self._assert_rejected(data, "invalid fields")
        data = close_input([{"known_month": "2026-01", "asset": make_asset(), "x": 1}])
        self._assert_rejected(data, "invalid fields")
        self._assert_rejected(close_input([revision("2026-01", make_asset(cost=True))]),
                              "cost_cents")

    def test_noncanonical_month_rejected(self):
        self._assert_rejected(close_input([], first="2026-1", as_of="2026-02"), "YYYY-MM")
        self._assert_rejected(close_input([revision("2026-13", make_asset())]), "invalid month")


class CliTests(unittest.TestCase):
    def run_cli(self, source, output):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run([sys.executable, "-m", "asset_close.cli",
                               "--source", str(source), "--output", str(output)],
                              cwd=ROOT, env=env, capture_output=True, text=True)

    def test_cli_success_atomic_output_and_existing_target_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            source.write_text(json.dumps(close_input([
                revision("2026-01", make_asset()),
            ])))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(set(report), {"first_close_month", "as_of_month",
                                           "journal", "periods"})
            stale = output.read_text()
            again = self.run_cli(source, output)
            self.assertEqual(again.returncode, 2)
            self.assertIn("asset-close", again.stderr)
            self.assertEqual(output.read_text(), stale)  # 旧目标保留
            self.assertEqual(list(Path(tmp).glob(".report-*")), [])  # 临时文件已清理

    def test_cli_invalid_input_exit_2_and_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            source.write_text(json.dumps(close_input([
                revision("2026-01", make_asset(cost=10, salvage=20)),
            ])))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_cli_rejects_oversized_and_nan_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.json"
            big.write_text('{"first_close_month":"2026-01","as_of_month":"2026-02",'
                           '"revisions":' + " " * (8 * 1024 * 1024) + "[]}")
            self.assertEqual(self.run_cli(big, Path(tmp) / "o.json").returncode, 2)
            nan = Path(tmp) / "nan.json"
            nan.write_text('{"first_close_month":"2026-01","as_of_month":"2026-02",'
                           '"revisions":[{"known_month":"2026-01","asset":'
                           '{"id":"a","category":"c","cost_cents":NaN,'
                           '"salvage_cents":0,"life_months":1,'
                           '"in_service_month":"2026-01","disposal":null}}]}')
            self.assertEqual(self.run_cli(nan, Path(tmp) / "o2.json").returncode, 2)


class BackwardCompatibilityTests(unittest.TestCase):
    """旧 asset-schedule 的输入、输出与行为不受影响。"""

    def test_v1_fixture_summary_unchanged(self):
        from asset_schedule.service import run as run_v1
        data = json.loads((ROOT / "fixtures" / "input.json").read_text())
        result = run_v1(data)
        self.assertEqual(result["summary"], {"assets": 3, "disposed": 1,
                         "closing_book_cents": 4000, "cumulative_depreciation_cents": 9000,
                         "disposal_gain_loss_cents": 500})

    def test_v1_cli_still_works(self):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "v1.json"
            result = subprocess.run([sys.executable, "-m", "asset_schedule.cli",
                                     "--source", str(ROOT / "fixtures" / "input.json"),
                                     "--output", str(output)],
                                    cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text())["schema_version"], 1)


class CrossImplementationTests(unittest.TestCase):
    """restated 最终口径与旧 v1 调度器共享同一套事实，可独立交叉验算。"""

    def test_restated_totals_equal_v1_schedule_of_final_versions(self):
        from asset_schedule.model import month
        from asset_schedule.depreciation import schedule as v1_schedule

        revisions = [
            revision("2026-01", make_asset("a", "equipment", cost=101, life=3,
                                           disposal={"month": "2026-02", "proceeds_cents": 60})),
            revision("2026-02", make_asset("b", "tools", cost=200, salvage=20, life=5,
                                           disposal=None)),
            revision("2026-08", make_asset("a", "equipment", cost=101, life=3,
                                           disposal={"month": "2026-02", "proceeds_cents": 55})),
        ]
        data = close_input(revisions, as_of="2026-06")
        report = run(data)

        finals = {}
        for rev in revisions:
            if rev["known_month"] <= "2026-06":
                finals[rev["asset"]["id"]] = rev["asset"]
        expected_cost = expected_dep = expected_removed = expected_proceeds = 0
        for asset in finals.values():
            result = v1_schedule(asset, month("2026-06"))
            expected_cost += result["recognized_cost_cents"]
            expected_dep += result["accumulated_cents"]
            if result["disposal_result"]:
                expected_removed += result["disposal_result"]["carrying_cents"]
                expected_proceeds += result["disposal_result"]["proceeds_cents"]

        restated_totals = {"cost": 0, "depreciation": 0, "carrying_removed": 0, "proceeds": 0}
        for period in report["periods"]:
            for values in period["restated"].values():
                restated_totals["cost"] += values["cost_cents"]
                restated_totals["depreciation"] += values["depreciation_cents"]
                restated_totals["carrying_removed"] += values["carrying_removed_cents"]
                restated_totals["proceeds"] += values["proceeds_cents"]
        self.assertEqual(restated_totals, {
            "cost": expected_cost, "depreciation": expected_dep,
            "carrying_removed": expected_removed, "proceeds": expected_proceeds})
        last_book = sum(period["restated"].get(cat, {}).get("closing_book_cents", 0)
                        for period in report["periods"][-1:]
                        for cat in period["restated"])
        self.assertEqual(last_book, expected_cost - expected_dep - expected_removed)


if __name__ == "__main__":
    unittest.main()
