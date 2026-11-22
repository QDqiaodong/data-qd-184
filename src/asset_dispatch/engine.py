"""Exact joint optimizer for flexible maintenance and usage requests.

Days are integer offsets from the planning-window start; per-asset day sets are
bit masks.  Base availability comes straight from the ``asset-availability``
engine's status segments, so the dispatch view of "available" can never drift
from the availability report.

The objective is lexicographic:

1. maximise accepted ``high`` requests, then accepted ``normal`` ones;
2. minimise the sum of |actual start - preferred start| over flexible orders;
3. smallest start-date vector (orders sorted by ID, dates ascending);
4. smallest assignment vector (requests sorted by ID; asset IDs ascending,
   a dropped request sorting after every asset).

Maintenance placement and request acceptance decouple in one direction only:
orders must be placed in full whatever happens, while requests may be dropped,
so the search enumerates feasible order placements (crew capacity and the
one-job-per-asset-per-day rule) and solves the request side exactly for each.
Because the start-date vector fully determines a placement, the assignment
tie-break only ever matters inside one placement, where a lexicographic
reconstruction pass yields the smallest assignment vector.
"""
from datetime import date, timedelta

from asset_availability.engine import build_details


def _base_free_days(validated):
    """Per-asset set of day offsets whose existing status is ``available``."""
    details, _monthly, _names = build_details(
        validated["report_from"], validated["report_to"], validated["assets"],
        validated["fixed_orders"], validated["incidents"])
    origin = validated["report_from"].toordinal()
    free = {}
    for row in details:
        days = set()
        for segment in row["segments"]:
            if segment["status"] == "available":
                start = date.fromisoformat(segment["start"]).toordinal() - origin
                end = date.fromisoformat(segment["end"]).toordinal() - origin
                days.update(range(start, end))
        free[row["id"]] = days
    return free


class _Problem:
    def __init__(self, validated):
        report_from = validated["report_from"]
        self.report_from = report_from
        self.origin = report_from.toordinal()
        self.days = (validated["report_to"] - report_from).days
        assets = validated["assets"]
        self.asset_ids = sorted(assets)
        self.asset_index = {asset_id: index
                            for index, asset_id in enumerate(self.asset_ids)}
        self.category = {asset_id: assets[asset_id]["category"]
                         for asset_id in self.asset_ids}
        crew = validated["crew"]
        self.crew = [crew[report_from + timedelta(days=offset)]
                     for offset in range(self.days)]
        free = _base_free_days(validated)
        self.free_mask = []
        for asset_id in self.asset_ids:
            mask = 0
            for offset in free[asset_id]:
                mask |= 1 << offset
            self.free_mask.append(mask)

        self.orders = []
        for raw in sorted(validated["orders"], key=lambda row: row["id"]):
            asset = self.asset_index[raw["asset_id"]]
            first = (raw["window_start"] - report_from).days
            length = (raw["window_end"] - raw["window_start"]).days
            duration = raw["duration_days"]
            preferred = (raw["preferred_start"] - report_from).days
            starts = []
            for start in range(first, first + length - duration + 1):
                mask = ((1 << duration) - 1) << start
                if mask & ~self.free_mask[asset]:
                    continue
                starts.append((start, mask, abs(start - preferred),
                               tuple(range(start, start + duration))))
            starts.sort(key=lambda option: (option[2], option[0]))
            self.orders.append({
                "id": raw["id"],
                "asset_id": raw["asset_id"],
                "asset": asset,
                "duration": duration,
                "preferred": preferred,
                "starts": starts,
            })

        self.requests = []
        for raw in sorted(validated["requests"], key=lambda row: row["id"]):
            start = (raw["start"] - report_from).days
            end = (raw["end"] - report_from).days
            mask = ((1 << (end - start)) - 1) << start
            eligible = [index for index, asset_id in enumerate(self.asset_ids)
                        if self.category[asset_id] == raw["category"]
                        and not mask & ~self.free_mask[index]]
            self.requests.append({
                "id": raw["id"],
                "high": raw["high"],
                "start": start,
                "end": end,
                "mask": mask,
                "eligible": eligible,
            })

    def iso(self, offset):
        return date.fromordinal(self.origin + offset).isoformat()


def _request_counts(problem, blocked, forced):
    """Best (high, normal) acceptance counts under one maintenance placement.

    ``blocked`` is the per-asset maintenance day mask; ``forced`` holds request
    indices (ID order) that must be accepted.  Returns None when some forced
    request cannot be served at all.

    The depth-first search is pruned by an upper bound on the counts still
    reachable: per priority class it takes the cheaper of two relaxations —
    the number of remaining requests with at least one free asset, and the sum
    over assets of a per-asset interval-scheduling capacity.  The second term
    collapses the permutation explosion of many similar requests sharing few
    assets, while the first stays tight when requests rarely conflict.
    """
    requests = problem.requests
    order = sorted(range(len(requests)),
                   key=lambda i: (i not in forced, not requests[i]["high"], i))
    used = [0] * len(problem.asset_ids)
    best = None

    def bound(position):
        """Upper bound on additionally acceptable (high, normal) counts."""
        place_high = place_normal = 0
        intervals = {True: [[] for _ in used], False: [[] for _ in used]}
        for j in order[position:]:
            req = requests[j]
            placeable = False
            for asset in req["eligible"]:
                if not req["mask"] & (blocked[asset] | used[asset]):
                    placeable = True
                    intervals[req["high"]][asset].append((req["start"], req["end"]))
            if placeable:
                if req["high"]:
                    place_high += 1
                else:
                    place_normal += 1
        caps = {}
        for high in (True, False):
            capacity = 0
            for asset in range(len(used)):
                free_since = -1
                for start, end in sorted(intervals[high][asset],
                                         key=lambda span: (span[1], span[0])):
                    if start >= free_since:
                        capacity += 1
                        free_since = end
            caps[high] = capacity
        return (min(place_high, caps[True]), min(place_normal, caps[False]))

    def recurse(position, high, normal):
        nonlocal best
        if position == len(order):
            if best is None or (high, normal) > best:
                best = (high, normal)
            return
        if best is not None:
            more_high, more_normal = bound(position)
            if (high + more_high, normal + more_normal) <= best:
                return
        index = order[position]
        req = requests[index]
        for asset in req["eligible"]:
            if req["mask"] & (blocked[asset] | used[asset]):
                continue
            used[asset] |= req["mask"]
            recurse(position + 1,
                    high + 1 if req["high"] else high,
                    normal if req["high"] else normal + 1)
            used[asset] &= ~req["mask"]
        if index not in forced:
            recurse(position + 1, high, normal)

    recurse(0, 0, 0)
    return best


def _lexmin_assignment(problem, blocked, counts, forced):
    """Smallest assignment vector (requests by ID, null last) meeting counts."""
    requests = problem.requests
    target_high, target_normal = counts
    used = [0] * len(problem.asset_ids)
    assignment = [None] * len(requests)
    result = None

    def recurse(index, high, normal):
        nonlocal result
        if result is not None:
            return
        if high > target_high or normal > target_normal:
            return
        if index == len(requests):
            if (high, normal) == (target_high, target_normal):
                result = list(assignment)
            return
        more_high = more_normal = 0
        for j in range(index, len(requests)):
            req = requests[j]
            if any(not req["mask"] & (blocked[a] | used[a]) for a in req["eligible"]):
                if req["high"]:
                    more_high += 1
                else:
                    more_normal += 1
        if high + more_high < target_high or normal + more_normal < target_normal:
            return
        req = requests[index]
        for asset in req["eligible"]:
            if req["mask"] & (blocked[asset] | used[asset]):
                continue
            used[asset] |= req["mask"]
            assignment[index] = asset
            recurse(index + 1,
                    high + 1 if req["high"] else high,
                    normal if req["high"] else normal + 1)
            used[asset] &= ~req["mask"]
            assignment[index] = None
            if result is not None:
                return
        if index not in forced:
            recurse(index + 1, high, normal)

    recurse(0, 0, 0)
    return result


def _optimize(problem, forced):
    """Best joint plan, or None when no feasible plan exists.

    Returns {"starts", "counts", "assignment"} aligned with the ID-sorted
    ``problem.orders`` / ``problem.requests``.
    """
    orders = problem.orders
    requests = problem.requests
    sequence = sorted(range(len(orders)),
                      key=lambda i: (len(orders[i]["starts"]), i))
    crew_left = list(problem.crew)
    asset_busy = [0] * len(problem.asset_ids)
    starts = [None] * len(orders)
    best = {}

    def min_remaining_shift(position):
        total = 0
        for k in range(position, len(sequence)):
            order = orders[sequence[k]]
            lowest = None
            for _start, mask, shift, days in order["starts"]:
                if mask & asset_busy[order["asset"]]:
                    continue
                if any(crew_left[day] == 0 for day in days):
                    continue
                lowest = shift
                break
            if lowest is None:
                return None
            total += lowest
        return total

    def count_bound():
        high = normal = 0
        for req in requests:
            if any(not req["mask"] & asset_busy[a] for a in req["eligible"]):
                if req["high"]:
                    high += 1
                else:
                    normal += 1
        return high, normal

    def recurse(position, shift_so_far):
        remaining = min_remaining_shift(position)
        if remaining is None:
            return
        if best:
            bound = count_bound()
            if bound < best["counts"]:
                return
            if (bound == best["counts"]
                    and shift_so_far + remaining > best["shift"]):
                return
        if position == len(sequence):
            counts = _request_counts(problem, asset_busy, forced)
            if counts is None:
                return
            key = (-counts[0], -counts[1], shift_so_far, tuple(starts))
            if not best or key < best["key"]:
                best.update(key=key, counts=counts, shift=shift_so_far,
                            starts=tuple(starts), blocked=tuple(asset_busy))
            return
        index = sequence[position]
        order = orders[index]
        asset = order["asset"]
        for start, mask, shift, days in order["starts"]:
            if mask & asset_busy[asset]:
                continue
            if any(crew_left[day] == 0 for day in days):
                continue
            asset_busy[asset] |= mask
            for day in days:
                crew_left[day] -= 1
            starts[index] = start
            recurse(position + 1, shift_so_far + shift)
            starts[index] = None
            for day in days:
                crew_left[day] += 1
            asset_busy[asset] &= ~mask

    recurse(0, 0)
    if not best:
        return None
    assignment = _lexmin_assignment(problem, best["blocked"], best["counts"], forced)
    return {"starts": best["starts"], "counts": best["counts"],
            "assignment": assignment}


def _score(problem, plan):
    high, normal = plan["counts"]
    shift = sum(abs(start - order["preferred"])
                for order, start in zip(problem.orders, plan["starts"]))
    return {"high_accepted": high, "normal_accepted": normal,
            "maintenance_shift_days": shift}


def _maintenance_rows(problem, starts):
    return [{"order_id": order["id"], "asset_id": order["asset_id"],
             "start": problem.iso(start),
             "end": problem.iso(start + order["duration"])}
            for order, start in zip(problem.orders, starts)]


def _assignment_rows(problem, assignment):
    return [{"request_id": req["id"],
             "asset_id": None if asset is None else problem.asset_ids[asset]}
            for req, asset in zip(problem.requests, assignment)]


def _maintenance_changes(problem, old_starts, new_starts):
    rows = []
    for order, old, new in zip(problem.orders, old_starts, new_starts):
        if old != new:
            rows.append({"order_id": order["id"],
                         "old_start": problem.iso(old),
                         "new_start": problem.iso(new)})
    return rows


def _assignment_changes(problem, old_assignment, new_assignment):
    rows = []
    for req, old, new in zip(problem.requests, old_assignment, new_assignment):
        if old != new:
            rows.append({
                "request_id": req["id"],
                "old_asset_id": None if old is None else problem.asset_ids[old],
                "new_asset_id": None if new is None else problem.asset_ids[new],
            })
    return rows


def solve(validated):
    """Build the dispatch report for an already-validated input."""
    problem = _Problem(validated)
    main = _optimize(problem, frozenset())
    if main is None:
        return {"status": "infeasible"}
    alternatives = []
    for index, req in enumerate(problem.requests):
        if main["assignment"][index] is not None:
            continue
        alt = _optimize(problem, frozenset({index}))
        if alt is None:
            alternatives.append({"request_id": req["id"], "status": "impossible"})
            continue
        alternatives.append({
            "request_id": req["id"],
            "status": "feasible",
            "score": _score(problem, alt),
            "maintenance_changes": _maintenance_changes(problem, main["starts"],
                                                        alt["starts"]),
            "assignment_changes": _assignment_changes(problem, main["assignment"],
                                                      alt["assignment"]),
        })
    return {
        "status": "planned",
        "score": _score(problem, main),
        "maintenance": _maintenance_rows(problem, main["starts"]),
        "assignments": _assignment_rows(problem, main["assignment"]),
        "alternatives": alternatives,
    }
