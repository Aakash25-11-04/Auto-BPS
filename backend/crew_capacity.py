"""Crew & resource capacity (Feature 10).

The optimizer used to place work with no notion of whether anyone exists to
DO it: if Engineering fields 4 maintenance gangs and the plan has 8
Engineering tasks running at 02:00, half of that plan is fiction. This
module is the single home for the crew model every scheduling path shares:

  - DepartmentCapacity rows -> CrewCapacity: "how many gangs can department
    D field simultaneously, across ALL corridors, on IST date X". A row with
    date=NULL is the department's standing default; a dated row overrides
    exactly that IST calendar day (00:00-24:00 IST).
  - The CP-SAT model (scheduler._solve_cp_sat) enforces it as ONE
    AddCumulative per department spanning every corridor. Per-date
    capacities are expressed inside that single constraint with fixed
    "reservation" intervals: the cumulative ceiling is the department's
    highest capacity over the horizon, and on a day whose capacity is lower
    a fixed interval covering that IST day consumes the difference. So a
    day with 1 SNT gang (ceiling 3) carries a fixed demand-2 reservation and
    only 1 unit remains for real work — still a single cumulative, no
    per-day constraint duplication.
  - The greedy fallback and every manual path (manual assign/swap, drag
    reschedule) use crew_conflict() below — the same count check, evaluated
    directly.
  - It is a HARD constraint everywhere. Unlike corridor concurrency (soft
    for a manual override), a manual override cannot exceed crew capacity:
    you cannot send a gang that doesn't exist. The COA must raise the
    number in the Admin panel first.
  - Gang NAMES (ENG-GANG-1...) are assigned post-solve by assign_gangs(), a
    greedy interval colouring; the model itself only enforces the count.

Time convention: every datetime here is naive-but-UTC (see tz_utils.py);
capacity lookups convert to the IST calendar date at the lookup point.
"""
import datetime as dt
import json
from collections import defaultdict

import models
from tz_utils import ist_date_to_utc_bounds, ist_today, to_ist

DEPARTMENTS = ["ENG", "TD", "SNT"]
DEFAULT_GANGS = {"ENG": 4, "TD": 3, "SNT": 3}
DEPARTMENT_NAMES = {"ENG": "Engineering", "TD": "Traction Distribution", "SNT": "Signal & Telecom"}
CREW_REASON_CODE = "crew_capacity"
HORIZON_DAYS = {"weekly": 7, "monthly": 30}


def ensure_default_capacity(db) -> int:
    """Seeds the standing per-department default (ENG 4, TD 3, SNT 3) for any
    department that has none yet. Idempotent; never overwrites an
    Admin-edited value."""
    existing = {
        r.department
        for r in db.query(models.DepartmentCapacity).filter(models.DepartmentCapacity.date.is_(None)).all()
    }
    created = 0
    for dept, gangs in DEFAULT_GANGS.items():
        if dept not in existing:
            db.add(models.DepartmentCapacity(
                department=dept, date=None, max_concurrent_gangs=gangs, notes="seeded default", updated_by="system",
            ))
            created += 1
    if created:
        db.commit()
    return created


class CrewCapacity:
    """In-memory snapshot of DepartmentCapacity, loaded once per solve so the
    model/greedy/reason passes all see identical numbers."""

    def __init__(self, defaults: dict, overrides: dict):
        self.defaults = dict(defaults)  # dept -> gangs
        self.overrides = dict(overrides)  # (dept, ist_date) -> (gangs, notes)

    @classmethod
    def load(cls, db) -> "CrewCapacity":
        ensure_default_capacity(db)
        defaults, overrides = {}, {}
        for r in db.query(models.DepartmentCapacity).all():
            if r.date is None:
                defaults[r.department] = r.max_concurrent_gangs
            else:
                overrides[(r.department, r.date)] = (r.max_concurrent_gangs, r.notes or "")
        return cls(defaults, overrides)

    def copy(self) -> "CrewCapacity":
        return CrewCapacity(self.defaults, self.overrides)

    def constrains(self, dept: str) -> bool:
        return dept in self.defaults or any(d == dept for d, _ in self.overrides)

    def capacity(self, dept: str, ist_date: dt.date):
        """Gangs available to `dept` on that IST date, or None if the
        department has no capacity configured at all (unconstrained)."""
        o = self.overrides.get((dept, ist_date))
        if o is not None:
            return o[0]
        return self.defaults.get(dept)

    def capacity_at(self, dept: str, when: dt.datetime):
        return self.capacity(dept, to_ist(when).date())

    def override_note(self, dept: str, ist_date: dt.date):
        o = self.overrides.get((dept, ist_date))
        return o[1] if o else None


# ------------------------------------------------------------ time helpers

def ist_midnights_between(start: dt.datetime, end: dt.datetime) -> list:
    """UTC instants of every IST midnight strictly inside (start, end) — the
    only points at which a department's capacity can change."""
    out = []
    d = to_ist(start).date() + dt.timedelta(days=1)
    while True:
        boundary, _ = ist_date_to_utc_bounds(d)
        if boundary >= end:
            break
        if boundary > start:
            out.append(boundary)
        d += dt.timedelta(days=1)
    return out


def ist_dates_spanned(start: dt.datetime, end: dt.datetime) -> list:
    first = to_ist(start).date()
    last = to_ist(end - dt.timedelta(microseconds=1)).date() if end > start else first
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


# ------------------------------------------------------------ the count check

def crew_conflict(existing: list, start: dt.datetime, end: dt.datetime, dept: str, crew: CrewCapacity):
    """Would ONE more gang of `dept` fit throughout [start, end), given the
    department's already-committed work `existing` [(start, end), ...] on
    any corridor? Returns None if it fits, else the first violation
    {at, capacity, committed}.

    Usage only rises at an interval start and capacity only changes at IST
    midnight, so checking `start`, every committed start inside the window,
    and every IST midnight inside it is exhaustive."""
    if end <= start:
        return None
    points = {start}
    points.update(s for s, _e in existing if start < s < end)
    points.update(ist_midnights_between(start, end))
    for p in sorted(points):
        cap = crew.capacity_at(dept, p)
        if cap is None:
            continue
        committed = sum(1 for s, e in existing if s <= p < e)
        if committed + 1 > cap:
            return {"at": p, "capacity": cap, "committed": committed}
    return None


def crew_feasible_start_exists(existing: list, w_start: dt.datetime, w_end: dt.datetime, duration: dt.timedelta, dept: str, crew: CrewCapacity) -> bool:
    """Is there ANY start time inside the window at which a gang is free for
    the whole duration? The earliest start of any feasible region is either
    the window start, a committed task's end, or an IST midnight (where
    capacity may rise) — so those are the only starts worth testing."""
    latest = w_end - duration
    if latest < w_start:
        return False
    candidates = {w_start}
    candidates.update(e for _s, e in existing if w_start <= e <= latest)
    candidates.update(m for m in ist_midnights_between(w_start, latest + dt.timedelta(microseconds=1)))
    return any(crew_conflict(existing, c, c + duration, dept, crew) is None for c in sorted(candidates))


# ------------------------------------------------------------ post-solve gang labelling

def assign_gangs(scheduled: list) -> dict:
    """Greedy interval colouring per department: sort by start time and give
    each task the lowest-numbered gang that is already free. For interval
    graphs this uses exactly as many gangs as the peak concurrency — three
    back-to-back tasks share ENG-GANG-1; four overlapping ones get GANG-1..4.
    A gang number can never exceed the concurrency at that task's start
    (gang k is only opened when gangs 1..k-1 are all busy), so it can never
    exceed the capacity the solver enforced either.

    `scheduled`: dicts with task_id, department, start, end. Returns
    {task_id: gang_id}."""
    by_dept = defaultdict(list)
    for e in scheduled:
        by_dept[e["department"]].append(e)
    result = {}
    for dept, items in by_dept.items():
        items.sort(key=lambda e: (e["start"], e["end"], e["task_id"]))
        gang_free_at = []
        for e in items:
            idx = next((i for i, free_at in enumerate(gang_free_at) if free_at <= e["start"]), None)
            if idx is None:
                gang_free_at.append(e["end"])
                idx = len(gang_free_at) - 1
            else:
                gang_free_at[idx] = e["end"]
            result[e["task_id"]] = f"{dept}-GANG-{idx + 1}"
    return result


def write_gang_assignments(db, plan_id: str, entries: list) -> dict:
    """(Re)builds GangAssignment rows for a plan from its entries (dicts with
    task_id, department, corridor_id, start, end). Returns {task_id: gang_id}."""
    db.query(models.GangAssignment).filter_by(plan_id=plan_id).delete()
    gangs = assign_gangs(entries)
    for e in entries:
        db.add(models.GangAssignment(
            plan_id=plan_id, task_id=e["task_id"], department=e["department"], gang_id=gangs[e["task_id"]],
            corridor_id=e["corridor_id"], assigned_window_start=e["start"], assigned_window_end=e["end"],
        ))
    return gangs


def gang_map(db, plan_id: str) -> dict:
    """{task_id: gang_id} for a plan. Plans materialized before gang labelling
    existed (or whose labels are otherwise out of step with their entries)
    are relabelled on first read — the labels are purely derived from the
    entries, so rebuilding them is always safe."""
    gangs = {g.task_id: g.gang_id for g in db.query(models.GangAssignment).filter_by(plan_id=plan_id).all()}
    entries = db.query(models.BlockPlanEntry).filter_by(plan_id=plan_id).all()
    if {e.task_id for e in entries} != set(gangs):
        gangs = write_gang_assignments(db, plan_id, [
            {"task_id": e.task_id, "department": e.department, "corridor_id": e.corridor_id,
             "start": e.assigned_window_start, "end": e.assigned_window_end}
            for e in entries
        ])
        db.commit()
    return gangs


# ------------------------------------------------------------ unscheduled reasons

def crew_unscheduled_reason(task, windows: list, crew: CrewCapacity, existing: list):
    """If NO candidate window has a start time at which one of the task's
    department gangs is free for the whole (weather-effective) duration,
    returns the specific crew-capacity reason; otherwise None (the task lost
    on corridor room / competition instead, which keeps its existing reason).

    windows: [(slot, duration: timedelta)] — every candidate window long
    enough to hold the task. existing: the department's committed
    [(start, end)] across ALL corridors in the solved plan."""
    dept = task.department
    if not windows or not crew.constrains(dept):
        return None
    for slot, duration in windows:
        if crew_feasible_start_exists(existing, slot.start_time, slot.end_time, duration, dept, crew):
            return None

    name = DEPARTMENT_NAMES.get(dept, dept)
    dates = sorted({d for slot, _dur in windows for d in ist_dates_spanned(slot.start_time, slot.end_time)})
    caps = {d: crew.capacity(dept, d) for d in dates}
    distinct = sorted({c for c in caps.values() if c is not None})
    overridden = [d for d in dates if crew.override_note(dept, d) is not None]
    override_txt = ""
    if overridden:
        override_txt = " Date overrides in effect: " + "; ".join(
            f"{d.strftime('%d %b')} = {caps[d]} gang{'s' if caps[d] != 1 else ''}"
            + (f" ({crew.override_note(dept, d)})" if crew.override_note(dept, d) else "")
            for d in overridden
        ) + "."

    corridor_txt = f"on this corridor ({task.corridor_id})"
    if len(distinct) == 1:
        n = distinct[0]
        if n == 0:
            return (f"{name} has 0 gangs available on this task's candidate dates; no crew can be sent during any "
                    f"candidate window for this task {corridor_txt}.{override_txt}")
        committed = f"all {n} are" if n != 1 else "its 1 gang is"
        return (f"{name} has {n} gang{'s' if n != 1 else ''} available; {committed} already committed during every "
                f"candidate window for this task {corridor_txt}.{override_txt}")
    return (f"{name} has {distinct[0]}-{distinct[-1]} gangs available across this task's candidate dates; every "
            f"available gang is already committed during every candidate window for this task {corridor_txt}."
            f"{override_txt}")


def manual_placement_violation(db, plan_id: str, task, start: dt.datetime, end: dt.datetime, exclude_entry_ids=()):
    """HARD check for every manual path. Returns a human-readable reason
    string if placing `task` at [start, end) in `plan_id` would exceed its
    department's gang capacity at any moment, else None."""
    crew = CrewCapacity.load(db)
    dept = task.department
    if not crew.constrains(dept):
        return None
    exclude = set(exclude_entry_ids or ())
    siblings = [
        e for e in db.query(models.BlockPlanEntry).filter_by(plan_id=plan_id, department=dept).all()
        if e.id not in exclude and e.task_id != task.task_id
    ]
    existing = [(e.assigned_window_start, e.assigned_window_end) for e in siblings]
    conflict = crew_conflict(existing, start, end, dept, crew)
    if not conflict:
        return None
    at = conflict["at"]
    busy = sorted(e.task_id for e in siblings if e.assigned_window_start <= at < e.assigned_window_end)
    name = DEPARTMENT_NAMES.get(dept, dept)
    cap = conflict["capacity"]
    at_ist = to_ist(at)
    return (
        f"crew capacity: {name} has {cap} gang{'s' if cap != 1 else ''} available on {at_ist.strftime('%d %b %Y')} and "
        f"{conflict['committed']} {'is' if conflict['committed'] == 1 else 'are'} already committed at "
        f"{at_ist.strftime('%H:%M')} IST ({', '.join(busy) or 'none'}) — crew capacity is a HARD limit that even a "
        f"manual override cannot exceed; increase {dept}'s capacity in the Admin panel first"
    )


# ------------------------------------------------------------ utilization / impact

def usage_profile(intervals: list, dept: str, crew: CrewCapacity) -> dict:
    """Peak simultaneous gangs, when it happened, capacity at that moment,
    and the worst usage/capacity ratio across the plan."""
    if not intervals:
        return {"peak_concurrent": 0, "peak_at": None, "capacity_at_peak": None, "utilization_pct": 0.0, "over_capacity": False}
    lo = min(s for s, _ in intervals)
    hi = max(e for _, e in intervals)
    points = sorted({s for s, _ in intervals} | set(ist_midnights_between(lo, hi)))
    peak, peak_at, cap_at_peak, worst = 0, None, None, 0.0
    over = False
    for p in points:
        used = sum(1 for s, e in intervals if s <= p < e)
        cap = crew.capacity_at(dept, p)
        if used > peak:
            peak, peak_at, cap_at_peak = used, p, cap
        if cap is not None:
            if used > cap:
                over = True
            ratio = (used / cap) if cap > 0 else (float("inf") if used else 0.0)
            worst = max(worst, ratio)
    util = 999.0 if worst == float("inf") else round(worst * 100, 1)
    return {"peak_concurrent": peak, "peak_at": peak_at, "capacity_at_peak": cap_at_peak, "utilization_pct": util, "over_capacity": over}


def tasks_over_capacity(items: list, dept: str, crew: CrewCapacity) -> list:
    """Minimum set of scheduled tasks that would have to come out for the
    department's work to fit `crew`. items: dicts with task_id, start, end.
    Sweep in time order; whenever the active count exceeds capacity, drop
    the active task that ends LAST (the classic exchange argument makes this
    optimal for a uniform capacity; with per-date capacities it is the same
    greedy applied per moment)."""
    if not items:
        return []
    lo = min(i["start"] for i in items)
    hi = max(i["end"] for i in items)
    points = sorted({i["start"] for i in items} | set(ist_midnights_between(lo, hi)))
    removed = set()
    for p in points:
        cap = crew.capacity_at(dept, p)
        if cap is None:
            continue
        active = [i for i in items if i["task_id"] not in removed and i["start"] <= p < i["end"]]
        while len(active) > cap:
            victim = max(active, key=lambda i: (i["end"], i["task_id"]))
            removed.add(victim["task_id"])
            active.remove(victim)
    return sorted(removed)


def active_plans(db) -> list:
    plans = []
    for horizon in HORIZON_DAYS:
        plan = (
            db.query(models.BlockPlan)
            .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
            .order_by(models.BlockPlan.version.desc())
            .first()
        )
        if plan:
            plans.append(plan)
    return plans


def capacity_change_impact(db, department: str, proposed: CrewCapacity) -> dict:
    """What a proposed capacity change would do to every currently active
    (draft/published) plan: how many already-scheduled tasks for that
    department would no longer fit. Tasks that are ALREADY over the current
    capacity are reported separately so the warning only counts what this
    change newly breaks."""
    current = CrewCapacity.load(db)
    per_plan = []
    total_new = 0
    for plan in active_plans(db):
        entries = db.query(models.BlockPlanEntry).filter_by(plan_id=plan.plan_id, department=department).all()
        # An entry whose task has since been cancelled/removed isn't work anyone will do.
        live_ids = {
            t.task_id for t in db.query(models.MaintenanceTask).filter(
                models.MaintenanceTask.task_id.in_([e.task_id for e in entries]),
                models.MaintenanceTask.status.notin_(["cancelled", "manually_removed", "displaced"]),
            ).all()
        } if entries else set()
        items = [{"task_id": e.task_id, "start": e.assigned_window_start, "end": e.assigned_window_end} for e in entries if e.task_id in live_ids]
        already = set(tasks_over_capacity(items, department, current))
        after = set(tasks_over_capacity(items, department, proposed))
        newly = sorted(after - already)
        total_new += len(newly)
        profile = usage_profile([(i["start"], i["end"]) for i in items], department, current)
        per_plan.append({
            "plan_id": plan.plan_id, "horizon": plan.horizon, "plan_status": plan.status,
            "scheduled_tasks": len(items), "current_peak_concurrent": profile["peak_concurrent"],
            "tasks_newly_infeasible": newly, "tasks_already_over_capacity": sorted(already),
        })
    return {"department": department, "tasks_newly_infeasible_count": total_new, "plans": per_plan}


def impact_message(department: str, gangs: int, date, impact: dict):
    n = impact["tasks_newly_infeasible_count"]
    if not n:
        return None
    when = f" on {date.strftime('%d %b %Y')}" if date else ""
    return (f"reducing {department} to {gangs} gang{'s' if gangs != 1 else ''}{when} would make {n} "
            f"currently-scheduled task{'s' if n != 1 else ''} infeasible")


def resource_summary(db, plan, entries: list, crew: CrewCapacity = None) -> list:
    """Per-department gang utilization for GET /schedule/plan and the
    Control Office resource strip. entries: BlockPlanEntry rows (unscoped —
    these are aggregates, not task-level detail)."""
    crew = crew or CrewCapacity.load(db)
    metrics = json.loads(plan.metrics_json or "{}")
    crew_limited = metrics.get("crew_limited_unscheduled", {}) or {}
    gangs = gang_map(db, plan.plan_id)

    limited_ids = {tid for ids in crew_limited.values() for tid in ids}
    still_unscheduled = set()
    if limited_ids:
        still_unscheduled = {
            t.task_id for t in db.query(models.MaintenanceTask).filter(
                models.MaintenanceTask.task_id.in_(limited_ids), models.MaintenanceTask.status == "unscheduled"
            ).all()
        }

    horizon_start = to_ist(plan.created_at).date() if plan.created_at else ist_today()
    horizon_end = horizon_start + dt.timedelta(days=HORIZON_DAYS.get(plan.horizon, 7))

    out = []
    for dept in DEPARTMENTS:
        dept_entries = [e for e in entries if e.department == dept]
        intervals = [(e.assigned_window_start, e.assigned_window_end) for e in dept_entries]
        profile = usage_profile(intervals, dept, crew)
        limited = sorted(t for t in crew_limited.get(dept, []) if t in still_unscheduled)
        util = profile["utilization_pct"]
        status = "red" if (limited or profile["over_capacity"]) else ("amber" if util >= 75 else "green")
        overrides = sorted(
            ({"date": d.isoformat(), "gangs": g, "notes": notes}
             for (od, d), (g, notes) in crew.overrides.items() if od == dept and horizon_start <= d <= horizon_end),
            key=lambda o: o["date"],
        )
        out.append({
            "department": dept,
            "gangs_available": crew.defaults.get(dept),
            "gangs_used": len({gangs[e.task_id] for e in dept_entries if e.task_id in gangs}),
            "peak_concurrent": profile["peak_concurrent"],
            "peak_at": profile["peak_at"],
            "capacity_at_peak": profile["capacity_at_peak"],
            "utilization_pct": util,
            "over_capacity": profile["over_capacity"],
            "crew_limited_unscheduled": limited,
            "status": status,
            "date_overrides": overrides,
        })
    return out
