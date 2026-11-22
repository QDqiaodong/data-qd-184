import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from asset_availability.service import run as availability_run
from asset_dispatch.service import run

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ORIGIN = date(2026, 4, 1)


def iso(offset):
    return (ORIGIN + timedelta(days=offset)).isoformat()


def asset(asset_id, category="crane", active_from="2026-04-01", active_until=None):
    return {"id": asset_id, "category": category,
            "active_from": active_from, "active_until": active_until}


def fixed(order_id, asset_id, start, end, executed=None):
    return {"id": order_id, "asset_id": asset_id, "planned_start": start,
            "planned_end": end,
            "executed_start": executed[0] if executed else None,
            "executed_end": executed[1] if executed else None}


def incident(incident_id, asset_id, start, end):
    return {"id": incident_id, "asset_id": asset_id, "start": start, "end": end}


def flex(order_id, asset_id, window, duration, preferred):
    return {"id": order_id, "asset_id": asset_id,
            "window_start": window[0], "window_end": window[1],
            "duration_days": duration, "preferred_start": preferred}


def request(request_id, start, end, category="crane", priority="normal"):
    return {"id": request_id, "category": category, "start": start, "end": end,
            "priority": priority}


def crew(days, value=1, overrides=None):
    result = {iso(offset): value for offset in range(days)}
    if overrides:
        result.update(overrides)
    return result


def dispatch(assets, crew_by_date, orders=(), requests=(), fixed_orders=(),
             incidents=(), to_date="2026-04-04"):
    return {"availability": {"from_date": "2026-04-01", "to_date": to_date,
                             "assets": assets,
                             "maintenance_orders": list(fixed_orders),
                             "incidents": list(incidents)},
            "crew_by_date": crew_by_date,
            "flexible_orders": list(orders),
            "requests": list(requests)}


def _better(plan, other):
    """Strict-better comparison implementing the documented objective order."""
    if plan[0] != other[0]:
        return plan[0] > other[0]                      # (high, normal) counts
    if plan[1] != other[1]:
        return plan[1] < other[1]                      # maintenance shift
    if plan[2] != other[2]:
        return plan[2] < other[2]                      # start-date vector
    mine = tuple((0, a) if a is not None else (1, "") for a in plan[3])
    theirs = tuple((0, a) if a is not None else (1, "") for a in other[3])
    return mine < theirs                               # assignment vector, null last


def brute_report(data):
    """Independent exponential reference implementation of the whole contract."""
    avail = availability_run(data["availability"])
    origin = date.fromisoformat(avail["from_date"]).toordinal()
    days = date.fromisoformat(avail["to_date"]).toordinal() - origin
    free, category = {}, {}
    for row in avail["assets"]:
        category[row["id"]] = row["category"]
        offsets = set()
        for segment in row["segments"]:
            if segment["status"] == "available":
                start = date.fromisoformat(segment["start"]).toordinal() - origin
                end = date.fromisoformat(segment["end"]).toordinal() - origin
                offsets.update(range(start, end))
        free[row["id"]] = offsets
    crew_left = {date.fromisoformat(key).toordinal() - origin: value
                 for key, value in data["crew_by_date"].items()}
    asset_ids = sorted(free)
    orders = sorted(data["flexible_orders"], key=lambda row: row["id"])
    requests = sorted(data["requests"], key=lambda row: row["id"])

    def offset(value):
        return date.fromisoformat(value).toordinal() - origin

    placements_per_order = []
    for order in orders:
        options = []
        last = offset(order["window_end"]) - order["duration_days"]
        for start in range(offset(order["window_start"]), last + 1):
            span = set(range(start, start + order["duration_days"]))
            if span <= free[order["asset_id"]]:
                options.append((start, span))
        placements_per_order.append(options)
    request_spans = [set(range(offset(req["start"]), offset(req["end"])))
                     for req in requests]

    def optimize(forced):
        best = None
        for placement in itertools.product(*placements_per_order):
            crew_used = [0] * days
            asset_used = set()
            feasible = True
            for order, (start, span) in zip(orders, placement):
                for day in span:
                    crew_used[day] += 1
                    if (crew_used[day] > crew_left[day]
                            or (order["asset_id"], day) in asset_used):
                        feasible = False
                    asset_used.add((order["asset_id"], day))
            if not feasible:
                continue
            options = []
            for index, req in enumerate(requests):
                eligible = [a for a in asset_ids
                            if category[a] == req["category"]
                            and request_spans[index] <= free[a]
                            and all((a, day) not in asset_used
                                    for day in request_spans[index])]
                options.append(eligible if index in forced else eligible + [None])
            for assignment in itertools.product(*options):
                used = set()
                clash = False
                for index, asset_id in enumerate(assignment):
                    if asset_id is None:
                        continue
                    for day in request_spans[index]:
                        if (asset_id, day) in used:
                            clash = True
                        used.add((asset_id, day))
                if clash:
                    continue
                high = sum(1 for req, a in zip(requests, assignment)
                           if a is not None and req["priority"] == "high")
                normal = sum(1 for req, a in zip(requests, assignment)
                             if a is not None and req["priority"] == "normal")
                shift = sum(abs(start - offset(order["preferred_start"]))
                            for order, (start, _span) in zip(orders, placement))
                plan = ((high, normal), shift,
                        tuple(start for start, _span in placement), assignment)
                if best is None or _better(plan, best):
                    best = plan
        return best

    main = optimize(frozenset())
    if main is None:
        return {"status": "infeasible"}

    def iso_date(value):
        return date.fromordinal(origin + value).isoformat()

    def score(plan):
        return {"high_accepted": plan[0][0], "normal_accepted": plan[0][1],
                "maintenance_shift_days": plan[1]}

    def maintenance(plan):
        return [{"order_id": order["id"], "asset_id": order["asset_id"],
                 "start": iso_date(start),
                 "end": iso_date(start + order["duration_days"])}
                for order, start in zip(orders, plan[2])]

    def assignments(plan):
        return [{"request_id": req["id"], "asset_id": asset_id}
                for req, asset_id in zip(requests, plan[3])]

    report = {"status": "planned", "score": score(main),
              "maintenance": maintenance(main), "assignments": assignments(main),
              "alternatives": []}
    for index, req in enumerate(requests):
        if main[3][index] is not None:
            continue
        alt = optimize(frozenset({index}))
        if alt is None:
            report["alternatives"].append({"request_id": req["id"],
                                           "status": "impossible"})
            continue
        entry = {"request_id": req["id"], "status": "feasible", "score": score(alt),
                 "maintenance_changes": [], "assignment_changes": []}
        for order, old, new in zip(orders, main[2], alt[2]):
            if old != new:
                entry["maintenance_changes"].append(
                    {"order_id": order["id"], "old_start": iso_date(old),
                     "new_start": iso_date(new)})
        for other, old, new in zip(requests, main[3], alt[3]):
            if old != new:
                entry["assignment_changes"].append(
                    {"request_id": other["id"], "old_asset_id": old,
                     "new_asset_id": new})
        report["alternatives"].append(entry)
    return report


class FixtureTests(unittest.TestCase):
    """The shipped synthetic fixture has a committed, hand-checked result."""

    def test_fixture_matches_expected_report(self):
        source = json.loads((ROOT / "fixtures" / "dispatch_input.json").read_text())
        expected = json.loads((ROOT / "fixtures" / "dispatch_expected.json").read_text())
        self.assertEqual(run(source), expected)

    def test_fixture_input_order_does_not_change_result(self):
        source = json.loads((ROOT / "fixtures" / "dispatch_input.json").read_text())
        expected = run(source)
        availability = source["availability"]
        shuffled = {
            "requests": list(reversed(source["requests"])),
            "flexible_orders": list(reversed(source["flexible_orders"])),
            "crew_by_date": dict(reversed(list(source["crew_by_date"].items()))),
            "availability": {
                "to_date": availability["to_date"],
                "from_date": availability["from_date"],
                "incidents": list(reversed(availability["incidents"])),
                "maintenance_orders": list(reversed(availability["maintenance_orders"])),
                "assets": list(reversed(availability["assets"])),
            },
        }
        self.assertEqual(run(shuffled), expected)


class DocExampleTests(unittest.TestCase):
    """任务书例子：4 月 1–4 日，A/B 同类，B 事故，H 高优先级，N 普通。"""

    def setUp(self):
        self.report = run(dispatch(
            assets=[asset("A", "machine"), asset("B", "machine")],
            crew_by_date=crew(3),
            orders=[flex("mo-a", "A", ("2026-04-02", "2026-04-04"), 1, "2026-04-02")],
            requests=[request("H", "2026-04-02", "2026-04-03", "machine", "high"),
                      request("N", "2026-04-03", "2026-04-04", "machine")],
            incidents=[incident("inc-b", "B", "2026-04-02", "2026-04-04")]))

    def test_main_plan_moves_maintenance_to_free_the_high_request(self):
        self.assertEqual(self.report, {
            "status": "planned",
            "score": {"high_accepted": 1, "normal_accepted": 0,
                      "maintenance_shift_days": 1},
            "maintenance": [{"order_id": "mo-a", "asset_id": "A",
                             "start": "2026-04-03", "end": "2026-04-04"}],
            "assignments": [{"request_id": "H", "asset_id": "A"},
                            {"request_id": "N", "asset_id": None}],
            "alternatives": [{
                "request_id": "N", "status": "feasible",
                "score": {"high_accepted": 0, "normal_accepted": 1,
                          "maintenance_shift_days": 0},
                "maintenance_changes": [{"order_id": "mo-a",
                                         "old_start": "2026-04-03",
                                         "new_start": "2026-04-02"}],
                "assignment_changes": [
                    {"request_id": "H", "old_asset_id": "A", "new_asset_id": None},
                    {"request_id": "N", "old_asset_id": None, "new_asset_id": "A"}],
            }],
        })


class CrewSharingTests(unittest.TestCase):
    def test_shared_crew_forces_one_order_off_its_preferred_day(self):
        report = run(dispatch(
            assets=[asset("A"), asset("B", "lift")],
            crew_by_date=crew(3),
            orders=[flex("o-a", "A", ("2026-04-01", "2026-04-04"), 1, "2026-04-01"),
                    flex("o-b", "B", ("2026-04-01", "2026-04-04"), 1, "2026-04-01")]))
        # 两台设备共享每天 1 个班组：一个订单必须让位；开工日向量取字典序最小。
        self.assertEqual(report["maintenance"], [
            {"order_id": "o-a", "asset_id": "A",
             "start": "2026-04-01", "end": "2026-04-02"},
            {"order_id": "o-b", "asset_id": "B",
             "start": "2026-04-02", "end": "2026-04-03"}])
        self.assertEqual(report["score"]["maintenance_shift_days"], 1)

    def test_multi_day_order_consumes_crew_on_every_day(self):
        report = run(dispatch(
            assets=[asset("A"), asset("B", "lift")],
            crew_by_date={"2026-04-01": 2, "2026-04-02": 1, "2026-04-03": 2},
            orders=[flex("o1", "A", ("2026-04-01", "2026-04-04"), 2, "2026-04-01"),
                    flex("o2", "B", ("2026-04-01", "2026-04-04"), 1, "2026-04-02")]))
        # o1 占 1–2 日；2 日只剩 1 个班组，o2 只能排在 1 日（向量最小）。
        self.assertEqual(report["maintenance"], [
            {"order_id": "o1", "asset_id": "A",
             "start": "2026-04-01", "end": "2026-04-03"},
            {"order_id": "o2", "asset_id": "B",
             "start": "2026-04-01", "end": "2026-04-02"}])
        self.assertEqual(report["score"]["maintenance_shift_days"], 1)


class RequestFeasibilityTests(unittest.TestCase):
    def test_free_asset_each_day_but_none_continuously_available(self):
        report = run(dispatch(
            assets=[asset("A"), asset("B")],
            crew_by_date=crew(2),
            incidents=[incident("i1", "A", "2026-04-01", "2026-04-02"),
                       incident("i2", "B", "2026-04-02", "2026-04-03")],
            requests=[request("r", "2026-04-01", "2026-04-03")],
            to_date="2026-04-03"))
        self.assertEqual(report["assignments"], [{"request_id": "r", "asset_id": None}])
        self.assertEqual(report["alternatives"],
                         [{"request_id": "r", "status": "impossible"}])

    def test_touching_intervals_may_share_one_asset(self):
        report = run(dispatch(
            assets=[asset("A")],
            crew_by_date=crew(2),
            requests=[request("r1", "2026-04-01", "2026-04-02"),
                      request("r2", "2026-04-02", "2026-04-03")],
            to_date="2026-04-03"))
        self.assertEqual(report["assignments"], [
            {"request_id": "r1", "asset_id": "A"},
            {"request_id": "r2", "asset_id": "A"}])
        self.assertEqual(report["score"]["normal_accepted"], 2)

    def test_unknown_category_is_dropped_and_impossible_to_force(self):
        report = run(dispatch(
            assets=[asset("A")], crew_by_date=crew(3),
            requests=[request("g", "2026-04-01", "2026-04-02", "ghost")]))
        self.assertEqual(report["assignments"], [{"request_id": "g", "asset_id": None}])
        self.assertEqual(report["alternatives"],
                         [{"request_id": "g", "status": "impossible"}])

    def test_planned_and_executed_fixed_maintenance_both_block(self):
        report = run(dispatch(
            assets=[asset("A")], crew_by_date=crew(3),
            fixed_orders=[fixed("f", "A", "2026-04-01", "2026-04-02",
                                executed=("2026-04-02", "2026-04-03"))],
            requests=[request("ra", "2026-04-03", "2026-04-04"),
                      request("rb", "2026-04-01", "2026-04-02"),
                      request("rc", "2026-04-02", "2026-04-03")]))
        self.assertEqual(report["assignments"], [
            {"request_id": "ra", "asset_id": "A"},
            {"request_id": "rb", "asset_id": None},
            {"request_id": "rc", "asset_id": None}])
        self.assertEqual(report["alternatives"], [
            {"request_id": "rb", "status": "impossible"},
            {"request_id": "rc", "status": "impossible"}])

    def test_asset_active_interval_limits_available_days(self):
        report = run(dispatch(
            assets=[asset("A", active_from="2026-04-02", active_until="2026-04-04")],
            crew_by_date=crew(3),
            requests=[request("early", "2026-04-01", "2026-04-03"),
                      request("late", "2026-04-02", "2026-04-04")]))
        self.assertEqual(report["assignments"], [
            {"request_id": "early", "asset_id": None},
            {"request_id": "late", "asset_id": "A"}])
        self.assertEqual(report["alternatives"],
                         [{"request_id": "early", "status": "impossible"}])


class TieBreakTests(unittest.TestCase):
    def test_equal_shift_picks_earliest_start_vector(self):
        report = run(dispatch(
            assets=[asset("A")], crew_by_date=crew(3),
            incidents=[incident("i", "A", "2026-04-02", "2026-04-03")],
            orders=[flex("o", "A", ("2026-04-01", "2026-04-04"), 1, "2026-04-02")]))
        # 首选日 4/2 被事故占用，4/1 与 4/3 偏移同为 1，取开工日较小者。
        self.assertEqual(report["maintenance"], [
            {"order_id": "o", "asset_id": "A",
             "start": "2026-04-01", "end": "2026-04-02"}])
        self.assertEqual(report["score"]["maintenance_shift_days"], 1)

    def test_assignment_vector_prefers_smaller_asset_and_drops_last(self):
        report = run(dispatch(
            assets=[asset("A"), asset("B")], crew_by_date=crew(1),
            requests=[request("r1", "2026-04-01", "2026-04-02", priority="high"),
                      request("r2", "2026-04-01", "2026-04-02"),
                      request("r3", "2026-04-01", "2026-04-02")],
            to_date="2026-04-02"))
        self.assertEqual(report["score"],
                         {"high_accepted": 1, "normal_accepted": 1,
                          "maintenance_shift_days": 0})
        self.assertEqual(report["assignments"], [
            {"request_id": "r1", "asset_id": "A"},
            {"request_id": "r2", "asset_id": "B"},
            {"request_id": "r3", "asset_id": None}])
        # 加单 r3：重排后 r2 落选、r3 接上，r1 保持 A。
        self.assertEqual(report["alternatives"], [{
            "request_id": "r3", "status": "feasible",
            "score": {"high_accepted": 1, "normal_accepted": 1,
                      "maintenance_shift_days": 0},
            "maintenance_changes": [],
            "assignment_changes": [
                {"request_id": "r2", "old_asset_id": "B", "new_asset_id": None},
                {"request_id": "r3", "old_asset_id": None, "new_asset_id": "B"}],
        }])


class InfeasibleTests(unittest.TestCase):
    def test_zero_crew_blocks_all_flexible_maintenance(self):
        report = run(dispatch(
            assets=[asset("A")], crew_by_date=crew(3, 0),
            orders=[flex("o", "A", ("2026-04-01", "2026-04-04"), 1, "2026-04-01")]))
        self.assertEqual(report, {"status": "infeasible"})

    def test_window_without_base_available_run_is_infeasible(self):
        report = run(dispatch(
            assets=[asset("A")], crew_by_date=crew(3),
            incidents=[incident("i", "A", "2026-04-01", "2026-04-04")],
            orders=[flex("o", "A", ("2026-04-01", "2026-04-04"), 1, "2026-04-01")]))
        self.assertEqual(report, {"status": "infeasible"})

    def test_two_orders_cannot_share_one_asset_day(self):
        report = run(dispatch(
            assets=[asset("A")], crew_by_date=crew(1, 2),
            orders=[flex("o1", "A", ("2026-04-01", "2026-04-02"), 1, "2026-04-01"),
                    flex("o2", "A", ("2026-04-01", "2026-04-02"), 1, "2026-04-01")],
            to_date="2026-04-02"))
        self.assertEqual(report, {"status": "infeasible"})

    def test_dropping_all_requests_still_needs_full_maintenance(self):
        # 检修可行但所有请求落选：正常 planned，加单分析全部 impossible。
        report = run(dispatch(
            assets=[asset("A")], crew_by_date=crew(2),
            orders=[flex("o", "A", ("2026-04-01", "2026-04-03"), 2, "2026-04-01")],
            requests=[request("r1", "2026-04-01", "2026-04-02"),
                      request("r2", "2026-04-02", "2026-04-03")],
            to_date="2026-04-03"))
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["score"], {"high_accepted": 0, "normal_accepted": 0,
                                           "maintenance_shift_days": 0})
        self.assertEqual(report["alternatives"], [
            {"request_id": "r1", "status": "impossible"},
            {"request_id": "r2", "status": "impossible"}])


class EmptyAndNamespaceTests(unittest.TestCase):
    def test_empty_orders_and_requests_plan_trivially(self):
        report = run(dispatch(assets=[asset("A")], crew_by_date=crew(3)))
        self.assertEqual(report, {
            "status": "planned",
            "score": {"high_accepted": 0, "normal_accepted": 0,
                      "maintenance_shift_days": 0},
            "maintenance": [], "assignments": [], "alternatives": []})

    def test_request_and_order_ids_are_separate_namespaces(self):
        report = run(dispatch(
            assets=[asset("A")], crew_by_date=crew(3),
            orders=[flex("same", "A", ("2026-04-01", "2026-04-02"), 1, "2026-04-01")],
            requests=[request("same", "2026-04-02", "2026-04-03")]))
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["assignments"],
                         [{"request_id": "same", "asset_id": "A"}])


class RandomizedCrossCheckTests(unittest.TestCase):
    """The optimizer must agree with the independent brute-force reference."""

    def random_input(self, rng):
        days = rng.randint(2, 4)
        assets = [asset(f"a{i}", rng.choice(("crane", "lift")))
                  for i in range(rng.randint(1, 3))]
        incidents = []
        for i in range(rng.randint(0, 2)):
            start = rng.randint(0, days - 1)
            incidents.append(incident(f"inc{i}", rng.choice(assets)["id"],
                                      iso(start), iso(rng.randint(start + 1, days))))
        fixed_orders = []
        for i in range(rng.randint(0, 1)):
            start = rng.randint(0, days - 1)
            fixed_orders.append(fixed(f"fix{i}", rng.choice(assets)["id"],
                                      iso(start), iso(rng.randint(start + 1, days))))
        orders = []
        for i in range(rng.randint(0, 2)):
            start = rng.randint(0, days - 1)
            end = rng.randint(start + 1, days)
            duration = rng.randint(1, end - start)
            orders.append(flex(f"mo{i}", rng.choice(assets)["id"],
                               (iso(start), iso(end)), duration,
                               iso(rng.randint(start, end - duration))))
        requests = []
        for i in range(rng.randint(1, 5)):
            start = rng.randint(0, days - 1)
            requests.append(request(
                f"rq{i}", iso(start), iso(rng.randint(start + 1, days)),
                rng.choice(("crane", "crane", "lift", "lift", "ghost")),
                rng.choice(("high", "normal"))))
        return dispatch(assets, crew(days, rng.choice((0, 1, 1, 2, 2))),
                        orders=orders, requests=requests,
                        fixed_orders=fixed_orders, incidents=incidents,
                        to_date=iso(days))

    def medium_input(self, rng):
        days = rng.randint(3, 5)
        assets = [asset(f"a{i}", rng.choice(("crane", "lift")))
                  for i in range(rng.randint(2, 4))]
        incidents = []
        for i in range(rng.randint(0, 2)):
            start = rng.randint(0, days - 1)
            incidents.append(incident(f"inc{i}", rng.choice(assets)["id"],
                                      iso(start), iso(rng.randint(start + 1, days))))
        orders = []
        for i in range(rng.randint(1, 3)):
            start = rng.randint(0, days - 1)
            end = rng.randint(start + 1, days)
            duration = rng.randint(1, end - start)
            orders.append(flex(f"mo{i}", rng.choice(assets)["id"],
                               (iso(start), iso(end)), duration,
                               iso(rng.randint(start, end - duration))))
        requests = []
        for i in range(rng.randint(2, 4)):
            start = rng.randint(0, days - 1)
            requests.append(request(
                f"rq{i}", iso(start), iso(rng.randint(start + 1, days)),
                rng.choice(("crane", "lift")), rng.choice(("high", "normal"))))
        return dispatch(assets, crew(days, rng.choice((1, 1, 2))),
                        orders=orders, requests=requests, incidents=incidents,
                        to_date=iso(days))

    def test_random_small_inputs_match_brute_force(self):
        rng = random.Random(1842026)
        for case in range(150):
            data = self.random_input(rng)
            with self.subTest(case=case, data=data):
                self.assertEqual(run(data), brute_report(data))

    def test_random_medium_inputs_match_brute_force(self):
        rng = random.Random(1840077)
        for case in range(40):
            data = self.medium_input(rng)
            with self.subTest(case=case, data=data):
                self.assertEqual(run(data), brute_report(data))


class CapacityTests(unittest.TestCase):
    def test_documented_capacity_runs_and_is_exact(self):
        days = 28
        assets = [asset(f"a{i:02d}") for i in range(12)]
        orders = [flex(f"m{i}", f"a{i:02d}", ("2026-04-01", "2026-04-29"), 1,
                       iso(i)) for i in range(10)]
        requests = [request(f"r{i}", iso(10 + i), iso(11 + i), priority="high")
                    for i in range(12)]
        report = run(dispatch(assets, crew(days, 12), orders=orders,
                              requests=requests, to_date="2026-04-29"))
        self.assertEqual(report["score"], {"high_accepted": 12, "normal_accepted": 0,
                                           "maintenance_shift_days": 0})
        self.assertEqual([row["start"] for row in report["maintenance"]],
                         [iso(i) for i in range(10)])
        self.assertEqual([row["asset_id"] for row in report["assignments"]],
                         ["a00"] * 12)
        self.assertEqual(report["alternatives"], [])


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, data, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            run(data)

    def base(self):
        return dispatch(assets=[asset("A")], crew_by_date=crew(3),
                        orders=[flex("o", "A", ("2026-04-01", "2026-04-04"), 1,
                                     "2026-04-01")],
                        requests=[request("r", "2026-04-01", "2026-04-02")])

    def test_root_fields(self):
        data = self.base()
        del data["requests"]
        self.assert_rejected(data, "invalid fields")
        data = self.base()
        data["extra"] = []
        self.assert_rejected(data, "invalid fields")
        data = self.base()
        data["availability"] = []
        self.assert_rejected(data, "availability requires an object")

    def test_availability_is_fully_validated(self):
        data = self.base()
        data["availability"]["unknown"] = 1
        self.assert_rejected(data, "invalid fields")
        data = self.base()
        data["availability"]["to_date"] = "2026-04-32"
        self.assert_rejected(data, "YYYY-MM-DD")

    def test_window_and_asset_capacity(self):
        data = self.base()
        data["availability"]["to_date"] = "2026-04-30"  # 29 天
        data["crew_by_date"] = crew(29)
        self.assert_rejected(data, "1..28")
        data["availability"]["to_date"] = "2026-04-29"  # 28 天可行
        data["crew_by_date"] = crew(28)
        self.assertEqual(run(data)["status"], "planned")
        data = self.base()
        data["availability"]["assets"] = [asset(f"a{i}") for i in range(13)]
        self.assert_rejected(data, "at most 12 assets")
        data["availability"]["assets"] = [asset(f"a{i}") for i in range(12)]
        data["requests"] = []
        data["flexible_orders"] = []
        self.assertEqual(run(data)["status"], "planned")

    def test_crew_by_date_must_cover_exactly_the_window(self):
        data = self.base()
        del data["crew_by_date"]["2026-04-03"]
        self.assert_rejected(data, "exactly each day")
        data = self.base()
        data["crew_by_date"]["2026-04-04"] = 1
        self.assert_rejected(data, "exactly each day")
        data = self.base()
        data["crew_by_date"]["2026-04-0x"] = 1
        self.assert_rejected(data, "YYYY-MM-DD")
        data = self.base()
        data["crew_by_date"] = [1, 1, 1]
        self.assert_rejected(data, "requires an object")

    def test_crew_values_are_bounded_integers(self):
        for bad in (-1, 13, 1.5, True, "1"):
            data = self.base()
            data["crew_by_date"]["2026-04-02"] = bad
            self.assert_rejected(data, "crew_by_date")
        data = self.base()
        data["crew_by_date"]["2026-04-02"] = 12
        self.assertEqual(run(data)["status"], "planned")

    def test_flexible_order_fields_and_ids(self):
        data = self.base()
        data["flexible_orders"][0]["extra"] = 1
        self.assert_rejected(data, "invalid fields")
        data = self.base()
        del data["flexible_orders"][0]["preferred_start"]
        self.assert_rejected(data, "invalid fields")
        data = self.base()
        data["flexible_orders"].append(flex("o", "A", ("2026-04-01", "2026-04-02"),
                                            1, "2026-04-01"))
        self.assert_rejected(data, "duplicate flexible order ID")
        data = self.base()
        data["availability"]["maintenance_orders"] = [
            fixed("f", "A", "2026-04-01", "2026-04-02")]
        data["flexible_orders"][0]["id"] = "f"
        self.assert_rejected(data, "duplicates a fixed maintenance order")
        data = self.base()
        data["flexible_orders"][0]["id"] = "  "
        self.assert_rejected(data, "nonempty string")
        data = self.base()
        data["flexible_orders"][0]["asset_id"] = "ghost"
        self.assert_rejected(data, "unknown asset")

    def test_flexible_order_window_duration_and_preferred(self):
        order = flex("o", "A", ("2026-04-02", "2026-04-02"), 1, "2026-04-02")
        data = self.base()
        data["flexible_orders"] = [order]
        self.assert_rejected(data, "window must be nonempty")
        data = self.base()
        data["flexible_orders"] = [flex("o", "A", ("2026-04-01", "2026-04-05"), 1,
                                        "2026-04-01")]
        self.assert_rejected(data, "within the planning window")
        for duration in (0, 4, True):
            data = self.base()
            data["flexible_orders"] = [flex("o", "A", ("2026-04-01", "2026-04-04"),
                                            duration, "2026-04-01")]
            self.assert_rejected(data, "duration_days")
        data = self.base()
        data["flexible_orders"] = [flex("o", "A", ("2026-04-01", "2026-04-04"), 2,
                                        "2026-04-03")]  # 工期溢出窗口
        self.assert_rejected(data, "preferred_start")
        data = self.base()
        data["flexible_orders"] = [flex("o", "A", ("2026-04-01", "2026-04-04"), 2,
                                        "2026-03-31")]
        self.assert_rejected(data, "preferred_start")

    def test_flexible_order_capacity(self):
        data = self.base()
        data["flexible_orders"] = [flex(f"o{i}", "A", ("2026-04-01", "2026-04-04"),
                                        1, "2026-04-01") for i in range(11)]
        self.assert_rejected(data, "at most 10")
        data["flexible_orders"] = data["flexible_orders"][:10]
        # 10 个订单同日同设备必然不可行，但输入本身合法。
        self.assertEqual(run(data), {"status": "infeasible"})

    def test_request_fields_and_ids(self):
        data = self.base()
        data["requests"][0]["extra"] = 1
        self.assert_rejected(data, "invalid fields")
        data = self.base()
        data["requests"].append(request("r", "2026-04-02", "2026-04-03"))
        self.assert_rejected(data, "duplicate request ID")
        data = self.base()
        data["requests"][0]["id"] = " "
        self.assert_rejected(data, "nonempty string")
        data = self.base()
        data["requests"][0]["category"] = ""
        self.assert_rejected(data, "nonempty string")
        for bad in ("HIGH", "High", 1, True, None):
            data = self.base()
            data["requests"][0]["priority"] = bad
            self.assert_rejected(data, 'must be "high" or "normal"')

    def test_request_interval(self):
        data = self.base()
        data["requests"][0]["end"] = data["requests"][0]["start"]
        self.assert_rejected(data, "interval must be nonempty")
        data = self.base()
        data["requests"][0]["end"] = "2026-04-05"
        self.assert_rejected(data, "within the planning window")
        data = self.base()
        data["requests"][0]["start"] = "2026-4-01"
        self.assert_rejected(data, "YYYY-MM-DD")
        data = self.base()
        data["requests"][0]["start"] = True
        self.assert_rejected(data, "YYYY-MM-DD")

    def test_request_capacity(self):
        data = self.base()
        data["requests"] = [request(f"r{i}", "2026-04-01", "2026-04-02")
                            for i in range(13)]
        self.assert_rejected(data, "at most 12")
        data["requests"] = data["requests"][:12]
        self.assertEqual(run(data)["status"], "planned")


class CliTests(unittest.TestCase):
    def run_cli(self, source, output):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run([sys.executable, "-m", "asset_dispatch.cli",
                               "--source", str(source), "--output", str(output)],
                              cwd=ROOT, env=env, capture_output=True, text=True)

    def test_cli_fixture_atomic_output_and_existing_target_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            result = self.run_cli(ROOT / "fixtures" / "dispatch_input.json", output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()),
                             json.loads((ROOT / "fixtures"
                                         / "dispatch_expected.json").read_text()))
            stale = output.read_text()
            again = self.run_cli(ROOT / "fixtures" / "dispatch_input.json", output)
            self.assertEqual(again.returncode, 2)
            self.assertIn("asset-dispatch", again.stderr)
            self.assertEqual(output.read_text(), stale)
            self.assertEqual(list(Path(tmp).glob(".report-*")), [])

    def test_cli_invalid_input_exit_2_and_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            source.write_text(json.dumps(self._invalid_input()))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    @staticmethod
    def _invalid_input():
        data = dispatch(assets=[asset("A")], crew_by_date=crew(3))
        data["crew_by_date"]["2026-04-02"] = 99
        return data

    def test_cli_infeasible_writes_status_only_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            data = dispatch(assets=[asset("A")], crew_by_date=crew(3, 0),
                            orders=[flex("o", "A", ("2026-04-01", "2026-04-04"), 1,
                                         "2026-04-01")])
            source.write_text(json.dumps(data))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()), {"status": "infeasible"})

    def test_cli_rejects_duplicate_keys_nan_and_oversized_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            duplicate = Path(tmp) / "dup.json"
            duplicate.write_text('{"availability":{},"availability":{}}')
            result = self.run_cli(duplicate, Path(tmp) / "o.json")
            self.assertEqual(result.returncode, 2)
            self.assertIn("duplicate JSON key", result.stderr)
            nan = Path(tmp) / "nan.json"
            nan.write_text('{"availability":{},"crew_by_date":{},"flexible_orders":[],'
                           '"requests":[],"x":NaN}')
            self.assertEqual(self.run_cli(nan, Path(tmp) / "o2.json").returncode, 2)
            big = Path(tmp) / "big.json"
            big.write_text('{"availability":' + " " * (8 * 1024 * 1024) + '{}')
            self.assertEqual(self.run_cli(big, Path(tmp) / "o3.json").returncode, 2)


class BackwardCompatibilityTests(unittest.TestCase):
    def test_existing_commands_unchanged(self):
        from asset_availability.service import run as run_availability
        from asset_close.service import run as run_close
        from asset_schedule.service import run as run_schedule
        schedule = json.loads((ROOT / "fixtures" / "input.json").read_text())
        self.assertEqual(run_schedule(schedule)["summary"]["closing_book_cents"], 4000)
        close = json.loads((ROOT / "fixtures" / "close_input.json").read_text())
        self.assertEqual(run_close(close)["as_of_month"], "2026-06")
        availability = json.loads((ROOT / "fixtures"
                                   / "availability_input.json").read_text())
        expected = json.loads((ROOT / "fixtures"
                               / "availability_expected.json").read_text())
        self.assertEqual(run_availability(availability), expected)


if __name__ == "__main__":
    unittest.main()
