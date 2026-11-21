import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from asset_availability.engine import order_metrics
from asset_availability.service import run

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
COUNTERS = ("active_days", "available_days", "maintenance_days", "incident_days",
            "unplanned_incident_days", "planned_maintenance_days",
            "executed_maintenance_days")


def asset(asset_id="dev", category="equipment", active_from="2026-01-01",
          active_until=None):
    return {"id": asset_id, "category": category, "active_from": active_from,
            "active_until": active_until}


def order(order_id="ord", asset_id="dev", planned=("2026-01-05", "2026-01-08"),
          executed=None):
    return {"id": order_id, "asset_id": asset_id,
            "planned_start": planned[0], "planned_end": planned[1],
            "executed_start": executed[0] if executed else None,
            "executed_end": executed[1] if executed else None}


def incident(incident_id="inc", asset_id="dev", span=("2026-01-06", "2026-01-08")):
    return {"id": incident_id, "asset_id": asset_id,
            "start": span[0], "end": span[1]}


def report(assets=None, orders=None, incidents=None,
           report_from="2026-01-01", report_to="2026-02-01"):
    return run({"from_date": report_from, "to_date": report_to,
                "assets": [] if assets is None else assets,
                "maintenance_orders": [] if orders is None else orders,
                "incidents": [] if incidents is None else incidents})


def segments(result, asset_id="dev"):
    return a_by_id(result, asset_id)["segments"]


def a_by_id(result, asset_id):
    return next(row for row in result["assets"] if row["id"] == asset_id)


def counters(result, month, category="equipment"):
    period = next(row for row in result["periods"] if row["month"] == month)
    return period["categories"].get(category)


class FixtureTests(unittest.TestCase):
    """The shipped synthetic fixture has a committed, hand-checked result."""

    def test_fixture_matches_expected_report(self):
        source = json.loads((ROOT / "fixtures" / "availability_input.json").read_text())
        expected = json.loads((ROOT / "fixtures" / "availability_expected.json").read_text())
        self.assertEqual(run(source), expected)

    def test_fixture_expected_report_is_internally_consistent(self):
        expected = json.loads((ROOT / "fixtures" / "availability_expected.json").read_text())
        totals = {name: 0 for name in COUNTERS}
        for period in expected["periods"]:
            self.assertEqual(list(period["categories"]),
                             sorted(period["categories"]))
            for values in period["categories"].values():
                self.assertEqual(values["active_days"],
                                 values["available_days"] + values["maintenance_days"]
                                 + values["incident_days"])
                for name in COUNTERS:
                    totals[name] += values[name]
        for name in ("active_days", "available_days", "maintenance_days",
                     "incident_days", "unplanned_incident_days"):
            self.assertEqual(expected["summary"][name], totals[name])
        for row in expected["assets"]:
            self.assertGreaterEqual(len(row["segments"]), 1)
            self._assert_contiguous(row["segments"])

    @staticmethod
    def _assert_contiguous(rows):
        for left, right in zip(rows, rows[1:]):
            assert left["end"] == right["start"], (left, right)
            assert left["status"] != right["status"], (left, right)
        assert rows[0]["start"] < rows[0]["end"]


class StatusSegmentTests(unittest.TestCase):
    def test_asset_effective_boundaries_clip_segments(self):
        result = report(
            assets=[asset(active_from="2025-12-20", active_until="2026-01-11")],
            report_from="2026-01-01", report_to="2026-02-01")
        self.assertEqual(segments(result), [
            {"start": "2026-01-01", "end": "2026-01-11", "status": "available"}])
        self.assertEqual(result["summary"]["active_days"], 10)
        self.assertEqual(counters(result, "2026-01")["active_days"], 10)
        # 报告区间结束但资产仍有效：段延伸到报告右边界。
        result = report(assets=[asset(active_until=None)])
        self.assertEqual(segments(result), [
            {"start": "2026-01-01", "end": "2026-02-01", "status": "available"}])

    def test_touching_intervals_merge_into_one_segment(self):
        result = report(
            assets=[asset()],
            orders=[order("a", planned=("2026-01-03", "2026-01-05")),
                    order("b", planned=("2026-01-05", "2026-01-08"))])
        self.assertEqual(segments(result), [
            {"start": "2026-01-01", "end": "2026-01-03", "status": "available"},
            {"start": "2026-01-03", "end": "2026-01-08", "status": "maintenance"},
            {"start": "2026-01-08", "end": "2026-02-01", "status": "available"}])
        self.assertEqual(counters(result, "2026-01")["maintenance_days"], 5)

    def test_incident_wins_over_planned_maintenance(self):
        result = report(
            assets=[asset()],
            orders=[order(planned=("2026-01-05", "2026-01-10"))],
            incidents=[incident(span=("2026-01-06", "2026-01-08"))])
        self.assertEqual(segments(result), [
            {"start": "2026-01-01", "end": "2026-01-05", "status": "available"},
            {"start": "2026-01-05", "end": "2026-01-06", "status": "maintenance"},
            {"start": "2026-01-06", "end": "2026-01-08", "status": "incident"},
            {"start": "2026-01-08", "end": "2026-01-10", "status": "maintenance"},
            {"start": "2026-01-10", "end": "2026-02-01", "status": "available"}])
        values = counters(result, "2026-01")
        self.assertEqual(values["incident_days"], 2)
        self.assertEqual(values["maintenance_days"], 3)
        self.assertEqual(values["planned_maintenance_days"], 5)
        self.assertEqual(values["unplanned_incident_days"], 0)

    def test_executed_interval_marks_maintenance_and_can_stand_alone(self):
        result = report(
            assets=[asset()],
            orders=[order(planned=("2026-01-20", "2026-01-21"),
                          executed=("2026-01-03", "2026-01-05"))])
        self.assertEqual(segments(result), [
            {"start": "2026-01-01", "end": "2026-01-03", "status": "available"},
            {"start": "2026-01-03", "end": "2026-01-05", "status": "maintenance"},
            {"start": "2026-01-05", "end": "2026-01-20", "status": "available"},
            {"start": "2026-01-20", "end": "2026-01-21", "status": "maintenance"},
            {"start": "2026-01-21", "end": "2026-02-01", "status": "available"}])
        values = counters(result, "2026-01")
        self.assertEqual(values["planned_maintenance_days"], 1)
        self.assertEqual(values["executed_maintenance_days"], 2)
        self.assertEqual(values["maintenance_days"], 3)

    def test_unplanned_incident_only_counts_days_outside_any_plan(self):
        result = report(
            assets=[asset()],
            orders=[order(planned=("2026-01-05", "2026-01-07"))],
            incidents=[incident(span=("2026-01-04", "2026-01-09"))])
        values = counters(result, "2026-01")
        self.assertEqual(values["incident_days"], 5)
        self.assertEqual(values["unplanned_incident_days"], 3)  # 1/4、1/7、1/8
        self.assertEqual(values["maintenance_days"], 0)

    def test_asset_outside_report_is_rejected_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "intersect"):
            report(assets=[asset(active_from="2026-02-01")])

    def test_single_day_report_and_maximum_span(self):
        one = report(assets=[asset()], incidents=[incident(span=("2026-01-05",
                                                                 "2026-01-06"))],
                     report_from="2026-01-05", report_to="2026-01-06")
        self.assertEqual(one["periods"], [{"month": "2026-01", "categories": {
            "equipment": {"active_days": 1, "available_days": 0,
                          "maintenance_days": 0, "incident_days": 1,
                          "unplanned_incident_days": 1,
                          "planned_maintenance_days": 0,
                          "executed_maintenance_days": 0}}}])
        self.assertEqual(one["assets"][0]["segments"], [
            {"start": "2026-01-05", "end": "2026-01-06", "status": "incident"}])
        start = date(2020, 1, 1)
        longest = report(assets=[asset(active_from="2020-01-01")],
                         report_from=start.isoformat(),
                         report_to=(start + timedelta(days=3660)).isoformat())
        self.assertEqual(longest["summary"]["active_days"], 3660)
        self.assertEqual(len(longest["periods"]), 121)

    def test_empty_input_keeps_months_with_zero_summary(self):
        result = report(report_from="2026-01-10", report_to="2026-03-02")
        self.assertEqual(result["assets"], [])
        self.assertEqual([period["month"] for period in result["periods"]],
                         ["2026-01", "2026-02", "2026-03"])
        self.assertTrue(all(period["categories"] == {} for period in result["periods"]))
        self.assertEqual(result["summary"], {"assets": 0, "orders": 0, "incidents": 0,
                                             "active_days": 0, "available_days": 0,
                                             "maintenance_days": 0, "incident_days": 0,
                                             "unplanned_incident_days": 0})

    def test_no_state_is_emitted_before_active_from_or_after_active_until(self):
        result = report(assets=[asset(active_from="2026-01-10",
                                      active_until="2026-01-20")])
        self.assertEqual(segments(result)[0]["start"], "2026-01-10")
        self.assertEqual(segments(result)[-1]["end"], "2026-01-20")
        self.assertEqual(result["summary"]["active_days"], 10)


class OrderMetricTests(unittest.TestCase):
    def test_full_within_window_and_partial_and_late(self):
        def metrics(planned, executed, report_to="2026-02-01"):
            built = order(planned=planned, executed=executed)
            return order_metrics({
                "id": "ord", "asset_id": "dev",
                "planned_start": date.fromisoformat(planned[0]),
                "planned_end": date.fromisoformat(planned[1]),
                "executed_start": date.fromisoformat(executed[0]) if executed else None,
                "executed_end": date.fromisoformat(executed[1]) if executed else None,
            }, date.fromisoformat(report_to))

        exact = metrics(("2026-01-05", "2026-01-10"), ("2026-01-05", "2026-01-10"))
        self.assertEqual((exact["status"], exact["planned_days"], exact["executed_days"],
                          exact["late_start_days"], exact["outside_plan_days"]),
                         ("completed_within_window", 5, 5, 0, 0))
        inside = metrics(("2026-01-05", "2026-01-10"), ("2026-01-06", "2026-01-08"))
        self.assertEqual(inside["status"], "completed_within_window")
        self.assertEqual((inside["late_start_days"], inside["outside_plan_days"]), (1, 0))
        early = metrics(("2026-01-10", "2026-01-12"), ("2026-01-08", "2026-01-11"))
        self.assertEqual((early["status"], early["late_start_days"],
                          early["outside_plan_days"]), ("completed_outside_window", 0, 2))
        late = metrics(("2026-01-05", "2026-01-07"), ("2026-01-06", "2026-01-11"))
        self.assertEqual((late["status"], late["late_start_days"],
                          late["outside_plan_days"]), ("completed_outside_window", 1, 4))
        shifted = metrics(("2026-01-05", "2026-01-10"), ("2026-01-08", "2026-01-13"))
        self.assertEqual((shifted["late_start_days"], shifted["outside_plan_days"]), (3, 3))

    def test_pending_and_missed_depend_on_report_end(self):
        built = order(planned=("2026-01-20", "2026-02-01"))
        pending = order_metrics({
            "id": "ord", "asset_id": "dev",
            "planned_start": date(2026, 1, 20), "planned_end": date(2026, 2, 1),
            "executed_start": None, "executed_end": None}, date(2026, 2, 1))
        self.assertEqual(pending["status"], "pending")
        self.assertEqual((pending["executed_days"], pending["late_start_days"],
                          pending["outside_plan_days"]), (0, 0, 0))
        missed = order_metrics({
            "id": "ord", "asset_id": "dev",
            "planned_start": date(2026, 1, 1), "planned_end": date(2026, 1, 20),
            "executed_start": None, "executed_end": None}, date(2026, 2, 1))
        self.assertEqual(missed["status"], "missed")

    def test_report_lists_orders_by_id_regardless_of_input_order(self):
        result = report(
            assets=[asset()],
            orders=[order("z", planned=("2026-01-10", "2026-01-11")),
                    order("a", planned=("2026-01-12", "2026-01-13"))])
        self.assertEqual([row["id"] for row in result["maintenance_orders"]], ["a", "z"])


class OverlapTests(unittest.TestCase):
    def test_overlapping_orders_do_not_double_count_days_but_keep_own_metrics(self):
        result = report(
            assets=[asset()],
            orders=[order("a", planned=("2026-01-05", "2026-01-09")),
                    order("b", planned=("2026-01-06", "2026-01-11"))])
        values = counters(result, "2026-01")
        self.assertEqual(values["maintenance_days"], 6)  # 1/5..1/10 一天一次
        self.assertEqual(values["planned_maintenance_days"], 6)
        self.assertEqual(result["summary"]["maintenance_days"], 6)
        self.assertEqual([(row["id"], row["planned_days"])
                          for row in result["maintenance_orders"]], [("a", 4), ("b", 5)])

    def test_overlapping_incidents_and_orders_count_union_once(self):
        result = report(
            assets=[asset()],
            orders=[order("a", planned=("2026-01-05", "2026-01-09"),
                          executed=("2026-01-06", "2026-01-12")),
                    order("b", planned=("2026-01-07", "2026-01-10"))],
            incidents=[incident("i1", span=("2026-01-08", "2026-01-12")),
                       incident("i2", span=("2026-01-10", "2026-01-14"))])
        values = counters(result, "2026-01")
        self.assertEqual(values["active_days"], 31)
        self.assertEqual(values["incident_days"], 6)          # 1/8..1/13
        self.assertEqual(values["available_days"], 31 - 9)    # 维护 1/5..1/13 共 9 天
        self.assertEqual(values["maintenance_days"], 3)       # 1/5..1/7
        self.assertEqual(values["planned_maintenance_days"], 5)
        self.assertEqual(values["executed_maintenance_days"], 6)
        # 计划维护并集 1/5..1/10 只解释事故日 1/8、1/9，其余 1/10..1/14 为非计划。
        self.assertEqual(values["unplanned_incident_days"], 4)
        self.assertEqual(values["available_days"] + values["maintenance_days"]
                         + values["incident_days"], values["active_days"])

    def test_two_categories_aggregate_independently(self):
        result = report(
            assets=[asset("a", "press", active_from="2026-01-01"),
                    asset("b", "lathe", active_from="2026-01-11"),
                    asset("c", "press", active_from="2026-01-21")],
            report_to="2026-02-01")
        january = {row["month"]: row["categories"] for row in result["periods"]}
        self.assertEqual(january["2026-01"]["press"]["active_days"], 31 + 11)
        self.assertEqual(january["2026-01"]["lathe"]["active_days"], 21)
        self.assertEqual(list(january["2026-01"]), ["lathe", "press"])


class PeriodTests(unittest.TestCase):
    def test_report_spanning_months_includes_empty_months(self):
        result = report(
            assets=[asset(active_from="2026-01-10", active_until="2026-01-20")],
            report_from="2025-12-30", report_to="2026-03-02")
        self.assertEqual([row["month"] for row in result["periods"]],
                         ["2025-12", "2026-01", "2026-02", "2026-03"])
        self.assertEqual(result["periods"][0]["categories"], {})
        self.assertEqual(result["periods"][-1]["categories"], {})
        self.assertEqual(result["periods"][2]["categories"], {})
        self.assertEqual(counters(result, "2026-01")["active_days"], 10)
        self.assertEqual(result["summary"]["active_days"], 10)

    def test_monthly_partial_days_follow_report_and_asset_bounds(self):
        result = report(
            assets=[asset(active_from="2026-01-20", active_until="2026-02-10")],
            report_from="2026-01-15", report_to="2026-02-15")
        january = counters(result, "2026-01")
        february = counters(result, "2026-02")
        self.assertEqual(january["active_days"], 12)   # 1/20..1/31
        self.assertEqual(february["active_days"], 9)   # 2/1..2/9
        self.assertEqual(result["summary"]["active_days"], 21)

    def test_category_and_input_order_do_not_change_output(self):
        assets = [asset("b", "press", active_from="2026-01-03"),
                  asset("a", "lathe", active_from="2026-01-01"),
                  asset("c", "press", active_from="2026-01-10")]
        orders = [order("z", "b", planned=("2026-01-05", "2026-01-08")),
                  order("a", "c", planned=("2026-01-11", "2026-01-13"))]
        incidents = [incident("i", "a", span=("2026-01-02", "2026-01-04"))]
        first = report(assets, orders, incidents)
        second = report(list(reversed(assets)), list(reversed(orders)),
                        list(reversed(incidents)))
        self.assertEqual(first, second)
        self.assertEqual([row["id"] for row in first["assets"]], ["a", "b", "c"])
        self.assertEqual([row["id"] for row in first["maintenance_orders"]], ["a", "z"])

    def test_summary_ties_out_with_all_month_and_asset_totals(self):
        result = report(
            assets=[asset("a", "press"), asset("b", "lathe",
                                              active_from="2026-01-10",
                                              active_until="2026-01-20")],
            orders=[order("o", "a", planned=("2026-01-05", "2026-01-08"),
                          executed=("2026-01-06", "2026-01-08"))],
            incidents=[incident("i", "a", span=("2026-01-06", "2026-01-07"))],
            report_from="2025-12-29", report_to="2026-02-02")
        totals = {name: 0 for name in COUNTERS}
        for period in result["periods"]:
            for values in period["categories"].values():
                for name in COUNTERS:
                    totals[name] += values[name]
        for name in ("active_days", "available_days", "maintenance_days",
                     "incident_days", "unplanned_incident_days"):
            self.assertEqual(result["summary"][name], totals[name])
        status_days = {"available": 0, "maintenance": 0, "incident": 0}
        for row in result["assets"]:
            for segment in row["segments"]:
                length = (date.fromisoformat(segment["end"])
                          - date.fromisoformat(segment["start"])).days
                status_days[segment["status"]] += length
        self.assertEqual(status_days["available"], result["summary"]["available_days"])
        self.assertEqual(status_days["maintenance"], result["summary"]["maintenance_days"])
        self.assertEqual(status_days["incident"], result["summary"]["incident_days"])
        self.assertEqual(sum(status_days.values()), result["summary"]["active_days"])


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, data, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            run(data)

    def base(self):
        return {"from_date": "2026-01-01", "to_date": "2026-02-01",
                "assets": [asset()], "maintenance_orders": [], "incidents": []}

    def test_missing_and_unknown_fields(self):
        data = self.base()
        del data["incidents"]
        self.assert_rejected(data, "invalid fields")
        data = self.base()
        data["extra"] = 1
        self.assert_rejected(data, "invalid fields")
        data = self.base()
        data["assets"][0]["extra"] = True
        self.assert_rejected(data, "invalid fields")
        data = self.base()
        data["maintenance_orders"] = [{"id": "o", "asset_id": "dev"}]
        self.assert_rejected(data, "invalid fields")

    def test_bad_dates_and_open_intervals(self):
        for value in ("2026-1-01", "2026-01-32", "2026-13-01", "01/02/2026", 5, True):
            data = self.base()
            data["from_date"] = value
            self.assert_rejected(data, "YYYY-MM-DD")
        data = self.base()
        data["from_date"] = data["to_date"]
        self.assert_rejected(data, "span 1..3660")
        data = self.base()
        data["to_date"] = (date.fromisoformat("2026-01-01")
                           + timedelta(days=3661)).isoformat()
        self.assert_rejected(data, "span 1..3660")
        data = self.base()
        data["assets"][0]["active_until"] = data["assets"][0]["active_from"]
        self.assert_rejected(data, "must be nonempty")
        data = self.base()
        data["maintenance_orders"] = [order(planned=("2026-01-05", "2026-01-05"))]
        self.assert_rejected(data, "must be nonempty")
        data = self.base()
        data["incidents"] = [incident(span=("2026-01-09", "2026-01-08"))]
        self.assert_rejected(data, "must be nonempty")

    def test_duplicate_ids_and_unknown_assets(self):
        data = self.base()
        data["assets"].append(asset("dev"))
        self.assert_rejected(data, "duplicate asset ID")
        data = self.base()
        data["maintenance_orders"] = [order("o"), order("o")]
        self.assert_rejected(data, "duplicate maintenance order ID")
        data = self.base()
        data["incidents"] = [incident("i"), incident("i")]
        self.assert_rejected(data, "duplicate incident ID")
        data = self.base()
        data["maintenance_orders"] = [order("o", asset_id="ghost")]
        self.assert_rejected(data, "unknown asset")
        data = self.base()
        data["incidents"] = [incident("i", asset_id="ghost")]
        self.assert_rejected(data, "unknown asset")

    def test_intervals_must_fall_in_asset_and_report(self):
        data = self.base()
        data["assets"] = [asset(active_from="2025-06-01")]
        data["maintenance_orders"] = [order(planned=("2025-12-31", "2026-01-02"))]
        self.assert_rejected(data, "report interval")
        data = self.base()
        data["maintenance_orders"] = [order(planned=("2026-01-31", "2026-02-02"))]
        self.assert_rejected(data, "report interval")
        data = self.base()
        data["assets"] = [asset(active_from="2026-01-05", active_until="2026-01-20")]
        data["maintenance_orders"] = [order(planned=("2026-01-04", "2026-01-06"))]
        self.assert_rejected(data, "asset active interval")
        data = self.base()
        data["assets"] = [asset(active_from="2026-01-05", active_until="2026-01-20")]
        data["incidents"] = [incident(span=("2026-01-19", "2026-01-21"))]
        self.assert_rejected(data, "asset active interval")

    def test_executed_pair_must_be_both_null_or_both_dates(self):
        data = self.base()
        data["maintenance_orders"] = [{"id": "o", "asset_id": "dev",
                                       "planned_start": "2026-01-05",
                                       "planned_end": "2026-01-08",
                                       "executed_start": "2026-01-06",
                                       "executed_end": None}]
        self.assert_rejected(data, "both be null")
        data = self.base()
        data["maintenance_orders"] = [order(executed=("2026-01-06", "2026-01-06"))]
        self.assert_rejected(data, "must be nonempty")
        data = self.base()
        data["maintenance_orders"] = [order(executed=("2026-02-01", "2026-02-03"))]
        self.assert_rejected(data, "report interval")

    def test_bool_and_nonfinite_numbers_are_rejected(self):
        data = self.base()
        data["assets"][0]["active_from"] = True
        self.assert_rejected(data, "YYYY-MM-DD")
        data = self.base()
        data["assets"][0]["id"] = 7
        self.assert_rejected(data, "nonempty string")

    def test_nonfinite_constants_rejected_by_loader(self):
        from asset_schedule.io import load
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.json"
            path.write_text('{"from_date":"2026-01-01","to_date":"2026-02-01",'
                            '"assets":[],"maintenance_orders":[],"incidents":[],'
                            '"x":NaN}')
            with self.assertRaises(ValueError):
                load(path)
            path.write_text('{"from_date":"2026-01-01","to_date":"2026-02-01",'
                            '"assets":[],"maintenance_orders":[],"incidents":[],'
                            '"x":1e999}')
            with self.assertRaisesRegex(ValueError, "non-finite"):
                run(load(path))


class CapacityTests(unittest.TestCase):
    def test_documented_capacity_is_accepted(self):
        start = date(2020, 1, 1)
        end = start + timedelta(days=3660)
        assets = [asset(f"a{i:03d}", f"c{i % 7}", active_from="2019-01-01")
                  for i in range(500)]
        orders = []
        for i in range(10_000):
            left = start + timedelta(days=(i * 37) % 3600)
            right = left + timedelta(days=1 + i % 5)
            orders.append(order(f"o{i:05d}", f"a{i % 500:03d}",
                                planned=(left.isoformat(), min(right, end).isoformat())))
        incidents = []
        for i in range(20_000):
            left = start + timedelta(days=(i * 53) % 3655)
            right = min(left + timedelta(days=1 + i % 3), end)
            incidents.append(incident(f"i{i:05d}", f"a{i % 500:03d}",
                                      span=(left.isoformat(), right.isoformat())))
        result = run({"from_date": start.isoformat(), "to_date": end.isoformat(),
                      "assets": assets, "maintenance_orders": orders,
                      "incidents": incidents})
        self.assertEqual(result["summary"]["assets"], 500)
        self.assertEqual(result["summary"]["orders"], 10_000)
        self.assertEqual(result["summary"]["incidents"], 20_000)
        self.assertEqual(len(result["periods"]), 121)


class CliTests(unittest.TestCase):
    def run_cli(self, source, output):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run([sys.executable, "-m", "asset_availability.cli",
                               "--source", str(source), "--output", str(output)],
                              cwd=ROOT, env=env, capture_output=True, text=True)

    def test_cli_fixture_atomic_output_and_existing_target_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            result = self.run_cli(ROOT / "fixtures" / "availability_input.json", output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()),
                             json.loads((ROOT / "fixtures"
                                         / "availability_expected.json").read_text()))
            stale = output.read_text()
            again = self.run_cli(ROOT / "fixtures" / "availability_input.json", output)
            self.assertEqual(again.returncode, 2)
            self.assertIn("asset-availability", again.stderr)
            self.assertEqual(output.read_text(), stale)
            self.assertEqual(list(Path(tmp).glob(".report-*")), [])

    def test_cli_invalid_input_exit_2_and_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            data = {"from_date": "2026-01-01", "to_date": "2026-02-01",
                    "assets": [asset(active_until="2025-12-01")],
                    "maintenance_orders": [], "incidents": []}
            source.write_text(json.dumps(data))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_cli_rejects_oversized_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "big.json"
            source.write_text('{"from_date":"2026-01-01","to_date":"2026-02-01",'
                              '"assets":[],"maintenance_orders":[],"incidents":'
                              + " " * (8 * 1024 * 1024) + "[]}")
            self.assertEqual(self.run_cli(source, Path(tmp) / "o.json").returncode, 2)

    def test_cli_rejects_duplicate_json_keys_and_nan(self):
        with tempfile.TemporaryDirectory() as tmp:
            duplicate = Path(tmp) / "dup.json"
            duplicate.write_text('{"from_date":"2026-01-01","to_date":"2026-02-01",'
                                 '"assets":[],"maintenance_orders":[],"incidents":[],'
                                 '"incidents":[]}')
            result = self.run_cli(duplicate, Path(tmp) / "o.json")
            self.assertEqual(result.returncode, 2)
            self.assertIn("duplicate JSON key", result.stderr)
            nan = Path(tmp) / "nan.json"
            nan.write_text('{"from_date":"2026-01-01","to_date":"2026-02-01",'
                           '"assets":[],"maintenance_orders":[],"incidents":[],'
                           '"x":NaN}')
            self.assertEqual(self.run_cli(nan, Path(tmp) / "o2.json").returncode, 2)


class BackwardCompatibilityTests(unittest.TestCase):
    def test_asset_schedule_unchanged(self):
        from asset_schedule.service import run as run_schedule
        fixture = json.loads((ROOT / "fixtures" / "input.json").read_text())
        self.assertEqual(run_schedule(fixture)["summary"],
                         {"assets": 3, "disposed": 1, "closing_book_cents": 4000,
                          "cumulative_depreciation_cents": 9000,
                          "disposal_gain_loss_cents": 500})

    def test_asset_close_unchanged(self):
        from asset_close.service import run as run_close
        fixture = json.loads((ROOT / "fixtures" / "close_input.json").read_text())
        result = run_close(fixture)
        self.assertEqual(set(result), {"first_close_month", "as_of_month",
                                       "journal", "periods"})
        self.assertEqual(result["first_close_month"], "2026-01")
        self.assertEqual(result["as_of_month"], "2026-06")
        self.assertEqual(len(result["periods"]), 6)

    def test_availability_does_not_touch_finance_schemas(self):
        from asset_availability.service import run as run_availability
        result = run_availability({"from_date": "2026-01-01",
                                   "to_date": "2026-02-01",
                                   "assets": [asset()],
                                   "maintenance_orders": [], "incidents": []})
        self.assertEqual(set(result), {"from_date", "to_date", "assets",
                                       "maintenance_orders", "periods", "summary"})
        self.assertNotIn("journal", result)


if __name__ == "__main__":
    unittest.main()
