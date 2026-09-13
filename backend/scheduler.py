"""Corridor block scheduling optimizer.

Models task placement as TRUE INTERVAL scheduling, not slot assignment: each
task gets a specific start/end time inside a corridor availability window,
so a 2-hour task no longer consumes an entire 6-hour window and blocks a
second task from using the remaining 4 hours. This is the core fix over the
earlier slot-assignment model, which capped "sharing" at 3 tasks per window
but made every sharer occupy the window's FULL duration regardless of how
long its own work actually took.

CP-SAT formulation, worked entirely in integer minutes from a fixed epoch
(the horizon start) — no floats or datetimes inside the model:
  - For each (task, candidate window) pair where the window's corridor
    matches the task's and the window is long enough, create an OPTIONAL
    interval variable (NewOptionalIntervalVar) with a start var, a fixed
    size (the task's required duration), an end var, and a presence
    literal. A task is "scheduled" iff exactly one of its candidate
    presence literals is true.
  - Per-corridor capacity uses AddCumulative (not AddNoOverlap) with a
    configurable capacity (default 3 concurrent work parties), because the
    whole point of ABPS is that several departments' crews CAN genuinely
    work the same corridor closure at the same time — AddNoOverlap would
    forbid that outright and defeat the coordination goal.
  - Mutual exclusion is enforced with AddNoOverlap over the *combined* pool
    of both tasks' candidate intervals (across every window either could
    land in), not by forbidding a shared slot — optional intervals that
    end up absent are automatically ignored by AddNoOverlap, so this
    correctly stops the two tasks from overlapping in time no matter which
    window each is ultimately placed in.
  - Coordination is redefined around genuine temporal overlap: for every
    pair of same-corridor tasks from different departments, a one-directional
    reified "overlap" boolean can only be set to 1 by the solver if both
    tasks are actually scheduled and their intervals actually overlap in
    time (eff_start/eff_end variables tie each task to whichever single
    candidate interval it ends up using). The objective rewards the solver
    for setting these, so it actively seeks real time-overlap, not just a
    shared nominal window.
  - A task interval can never overlap a real train movement, because every
    candidate window was already filtered to be timetable-safe end to end
    (see _slot_is_safe) and a task's interval is constrained to sit fully
    inside its chosen window.

Falls back to a deterministic greedy heuristic (also true interval
placement, not whole-window assignment) if OR-Tools is unavailable or the
solve throws, so the system never hard-fails.
"""
import datetime as dt
import json
import time
import uuid

from sqlalchemy.orm import Session

import crew_capacity
import metrics as metrics_mod
import models
import timetable_loader
import weather_service
from tz_utils import ist_date_to_utc_bounds, ist_today, to_ist, utc_now

try:
    # Imported eagerly on the main thread at process startup. OR-Tools'
    # native DLL loader (WinDLL on Windows) can fail with WinError 127 when
    # the *first* import of ortools happens inside a FastAPI worker thread
    # (run_in_threadpool) rather than the main thread; importing it once here
    # avoids that and lets every request just hit the already-loaded module.
    from ortools.sat.python import cp_model as _cp_model
except Exception:
    _cp_model = None

DEPARTMENTS = ["ENG", "TD", "SNT"]
DEFAULT_CORRIDOR_CAPACITY = 3  # max concurrent work parties allowed on one corridor at once

# Manual overrides (FR-COA-03) treat corridor concurrency as a SOFT limit —
# a COA/ADMIN can knowingly push a window past DEFAULT_CORRIDOR_CAPACITY and
# just get a warning, because that's a real crew/resource judgment call the
# Control Office is entitled to make. This ceiling is the one hard stop even
# a manual override cannot cross silently: beyond it, there is no room left
# to just "add one more" and the caller must explicitly displace an existing
# task via manual_swap_tasks rather than the system quietly bumping someone.
MANUAL_OVERRIDE_HARD_CONCURRENCY_CEILING = DEFAULT_CORRIDOR_CAPACITY + 2
COORDINATION_BONUS = 40.0  # objective points per genuinely-overlapping cross-department task pair
SCALE = 100  # CP-SAT needs integer coefficients; scores carry 1 decimal of meaning
SOLVE_TIME_LIMIT_SECONDS = 20.0  # leaves headroom under the 30s requirement for
# model-build + plan-materialization overhead, which CP-SAT's own time limit
# does not cover — measured up to ~5s on a 31-task/60-window monthly instance

HORIZON_DAYS = {"weekly": 7, "monthly": 30}

# Signal-clearance / safety margin enforced around every real train movement
# when validating candidate windows. Kept in step with
# vacancy.DEFAULT_BUFFER_MINUTES: vacancy.py applies it when COMPUTING
# windows, and _slot_is_safe re-applies it to every window regardless of
# provenance (manual, ML-forecast, or computed by an older revision).
SAFETY_BUFFER_MINUTES = 15


def _duration_hours(slot) -> float:
    return (slot.end_time - slot.start_time).total_seconds() / 3600.0


def _slot_is_safe(slot, occurrences: list, buffer_minutes: int = SAFETY_BUFFER_MINUTES) -> bool:
    """Re-validates a candidate slot against the real timetable directly
    (not just trusting that it was derived from a gap) — this is what
    catches a manually-entered COA window that happens to clash with a
    real train movement. Because a task's interval is always constrained to
    sit fully inside whichever window it's placed in, filtering out unsafe
    windows here is sufficient to guarantee no interval ever overlaps a real
    train movement — no further per-interval train check is needed.

    The SAFETY BUFFER is applied here, at the single point every candidate
    window passes through, rather than only in vacancy.py's derivation.
    That matters because the candidate pool also contains windows this
    system did NOT compute — manually entered ones, ML-forecast ones, and
    any left over from an earlier revision of the derivation — and those
    must be held to the same signal-clearance margin as a freshly computed
    one. A window touching a train movement within the buffer is not a
    usable maintenance window regardless of where it came from.

    Takes the corridor's already-expanded occurrence list rather than
    computing it itself — see get_safe_slots for why that distinction
    matters a great deal at scale."""
    buffer = dt.timedelta(minutes=buffer_minutes)
    for occ in occurrences:
        if (occ["start"] - buffer) < slot.end_time and slot.start_time < (occ["end"] + buffer):
            return False
    return True


def get_safe_slots(db: Session, horizon: str, horizon_start: dt.date = None):
    """Returns (safe_slots, unsafe_excluded_count) for a horizon — shared by
    the real optimizer and by baseline.py's manual-process simulation so
    both draw from exactly the same, timetable-validated pool of windows.

    Expands each distinct corridor's real train occurrences exactly ONCE
    and reuses that list for every slot on that corridor, rather than
    recomputing the full horizon's occurrence expansion per slot. That
    distinction is not a micro-optimization: with the ML-forecast layer
    (Layer 3B) adding dozens of extra candidate slots per corridor, the
    naive per-slot approach measured at 13.6s for 90 monthly-horizon slots
    on its own — enough on top of the CP-SAT solve to blow through the 30s
    budget. Per-corridor caching makes this call sub-second regardless of
    how many slots a corridor has, because the expensive part (expanding
    real train movements across the horizon) now happens once per corridor,
    not once per slot."""
    if horizon not in HORIZON_DAYS:
        raise ValueError("horizon must be 'weekly' or 'monthly'")
    horizon_days = HORIZON_DAYS[horizon]
    horizon_start = horizon_start or ist_today()  # "this week"/"today" means IST, not server-local or UTC
    all_slots = (
        db.query(models.CorridorSlot)
        .filter(models.CorridorSlot.horizon == horizon, models.CorridorSlot.status != "cancelled")
        .all()
    )
    occurrences_by_corridor = {}
    safe_slots = []
    for s in all_slots:
        if s.corridor_id not in occurrences_by_corridor:
            occurrences_by_corridor[s.corridor_id] = timetable_loader.corridor_occurrences(
                db, s.corridor_id, horizon_start, horizon_days
            )
        if _slot_is_safe(s, occurrences_by_corridor[s.corridor_id]):
            safe_slots.append(s)
    return safe_slots, len(all_slots) - len(safe_slots)


def _build_reason(task, eligible_slots_exist: bool, weather_reason: str = None) -> str:
    if not eligible_slots_exist:
        if weather_reason:
            return weather_reason
        return (
            f"No corridor slot on {task.corridor_id} long enough for the required "
            f"{task.required_duration_hours:g}h within this horizon (corridor unavailability)."
        )
    return "Lower relative priority than competing tasks contending for the same available slots."


def _minutes(when: dt.datetime, epoch: dt.datetime) -> int:
    return int(round((when - epoch).total_seconds() / 60.0))


def _from_minutes(minutes: int, epoch: dt.datetime) -> dt.datetime:
    return epoch + dt.timedelta(minutes=minutes)


def _apply_manual_pins(db: Session, horizon: str, tasks: list, safe_slots: list, eligible: dict, effective_duration: dict, crew=None) -> tuple:
    """FR-COA-03's "preserve manual overrides" behavior: every task with an
    override=True entry in this horizon's currently active plan (draft or
    published) is pinned to EXACTLY that window before the solver runs, by
    giving it exactly one candidate — a synthetic CorridorSlot standing in
    for its already-approved manual placement — and returning its task_id
    in pinned_task_ids so _solve_cp_sat can force that candidate's presence
    to 1 (a hard constraint the solver must satisfy) and _solve_greedy can
    seed it into `placed` before anything else is considered. Both solvers'
    existing capacity/mutual-exclusion machinery then naturally works AROUND
    the pin, because the pin is just another interval in the same pools —
    no separate code path needed for "don't move this one".

    Mutates safe_slots/eligible/effective_duration in place (appends the
    synthetic slot, overwrites that task's eligible list, adds the duration
    entry) rather than returning new copies, since the caller needs those
    same mutated structures passed on to the solver either way.

    Returns (pinned_task_ids: set, override_meta: {task_id: {override_reason,
    override_by, override_at}}, released: list) — override_meta is threaded
    through to _materialize_plan so the new plan's entry carries the SAME
    override provenance forward, not a blank one.

    Crew capacity (Feature 10) is HARD even for a manual override, and a
    pin is forced present in the model — so a pin set that exceeds a
    department's gang capacity (e.g. the Admin lowered the number after the
    overrides were made) would make the whole model infeasible. Pins are
    therefore admitted in the order they were made (override_at); a pin
    that would exceed its department's capacity is NOT pinned and competes
    as an ordinary task instead, and is reported in `released` so the run's
    metrics say so explicitly rather than silently dropping the pin."""
    plan = (
        db.query(models.BlockPlan)
        .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
        .order_by(models.BlockPlan.version.desc())
        .first()
    )
    pinned_task_ids = set()
    override_meta = {}
    released = []
    if not plan:
        return pinned_task_ids, override_meta, released

    tasks_by_id = {t.task_id: t for t in tasks}
    overridden_entries = (
        db.query(models.BlockPlanEntry)
        .filter(models.BlockPlanEntry.plan_id == plan.plan_id, models.BlockPlanEntry.override.is_(True))
        .all()
    )
    overridden_entries.sort(key=lambda e: (e.override_at or dt.datetime.min, e.id))
    admitted_by_dept = {}
    for entry in overridden_entries:
        task = tasks_by_id.get(entry.task_id)
        if not task:
            continue  # task no longer in the free-solve population (e.g. deactivated) — nothing to pin
        if crew is not None:
            committed = admitted_by_dept.setdefault(task.department, [])
            window = (entry.assigned_window_start, entry.assigned_window_end)
            if crew_capacity.crew_conflict(committed, window[0], window[1], task.department, crew):
                released.append({
                    "task_id": task.task_id, "department": task.department,
                    "reason": "pinned manual override would exceed department gang capacity — competing as a normal task",
                })
                continue
            committed.append(window)
        pin_slot = models.CorridorSlot(
            slot_id=f"pinned-{entry.task_id}", corridor_id=entry.corridor_id,
            start_time=entry.assigned_window_start, end_time=entry.assigned_window_end,
            status="used", derived_from="manual_override", horizon=horizon,
        )
        safe_slots.append(pin_slot)
        eligible[task.task_id] = [pin_slot]
        effective_duration[(task.task_id, pin_slot.slot_id)] = {
            "base_hours": task.required_duration_hours,
            "effective_hours": (entry.assigned_window_end - entry.assigned_window_start).total_seconds() / 3600.0,
            "buffer_pct": 0.0, "note": "pinned manual override — preserved across this re-run",
        }
        pinned_task_ids.add(task.task_id)
        override_meta[task.task_id] = {
            "override_reason": entry.override_reason, "override_by": entry.override_by, "override_at": entry.override_at,
        }
    return pinned_task_ids, override_meta, released


def run_schedule(
    db: Session,
    horizon: str,
    horizon_start: dt.date = None,
    corridor_capacity: int = DEFAULT_CORRIDOR_CAPACITY,
    previous_assignment: dict = None,
    stability_bonus: float = 0.0,
    preserve_manual_overrides: bool = True,
) -> dict:
    """previous_assignment/stability_bonus implement the emergency
    re-optimization deviation penalty (Layer 5): when re-solving after an
    urgent defect is injected, passing in the currently-published plan's
    {task_id: (start_minutes, end_minutes)} and a positive stability_bonus
    rewards the solver for keeping each task exactly where it already was,
    so the new plan only moves work when the emergency's own priority
    (plus whatever it displaces) genuinely outweighs the disruption —
    published plans don't shuffle just because a re-solve happened to find
    a marginally different optimum.

    preserve_manual_overrides (default True): every manually-placed block
    (see manual_assign_task/manual_swap_tasks) in the horizon's current plan
    is PINNED — the solver is forced to keep it exactly where it is and
    works around it, rather than treating it as just another free variable
    it might silently move or drop. Set False to let this run reconsider
    manually-placed tasks exactly like any other — an explicit, visible
    opt-out (the frontend surfaces this as a checkbox next to "Run
    Scheduler", defaulted ON) rather than a silent side effect of re-running."""
    if horizon not in HORIZON_DAYS:
        raise ValueError("horizon must be 'weekly' or 'monthly'")
    horizon_days = HORIZON_DAYS[horizon]
    horizon_start = horizon_start or ist_today()  # "this week"/"today" means IST, not server-local or UTC

    tasks = (
        db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.status.in_(["submitted", "unscheduled", "scheduled"]))
        .all()
    )
    safe_slots, unsafe_excluded = get_safe_slots(db, horizon, horizon_start)

    # Weather-aware candidate generation (Layer 4): corridor-match first
    # (duration not yet checked — a soft weather buffer can change the
    # effective duration a slot must satisfy), then let weather_service
    # apply the hard safety exclusion and compute the per-(task,slot)
    # effective duration in one pass. See weather_service.assess_candidates.
    corridor_matched = {t.task_id: [s for s in safe_slots if s.corridor_id == t.corridor_id] for t in tasks}
    weather = weather_service.assess_candidates(db, tasks, corridor_matched, horizon_start, horizon_days)
    eligible = weather["eligible"]
    effective_duration = weather["effective_duration"]
    weather_exclusion_reason = weather["weather_exclusion_reason"]

    # Department gang capacity (Feature 10) — one snapshot shared by the pin
    # admission check, both solvers, and the unscheduled-reason pass.
    crew = crew_capacity.CrewCapacity.load(db)

    pinned_task_ids, override_meta, released_pins = (
        _apply_manual_pins(db, horizon, tasks, safe_slots, eligible, effective_duration, crew=crew)
        if preserve_manual_overrides else (set(), {}, [])
    )

    # epoch is the UTC instant of IST midnight on horizon_start — NOT naive
    # local midnight — so the minute-offset arithmetic below (_minutes/
    # _from_minutes) stays consistent with every naive-but-UTC slot/
    # occurrence time it's compared against (see tz_utils.py).
    epoch, _ = ist_date_to_utc_bounds(horizon_start)
    previous_assignment_min = None
    if previous_assignment:
        previous_assignment_min = {
            task_id: (_minutes(s, epoch), _minutes(e, epoch)) for task_id, (s, e) in previous_assignment.items()
        }

    start = time.time()
    try:
        assignment, engine_used, solver_status = _solve_cp_sat(
            tasks, safe_slots, eligible, epoch, corridor_capacity, previous_assignment_min, stability_bonus,
            effective_duration=effective_duration, pinned_task_ids=pinned_task_ids, crew=crew,
        )
    except Exception:
        assignment, engine_used, solver_status = _solve_greedy(
            tasks, safe_slots, eligible, epoch, corridor_capacity, effective_duration=effective_duration,
            pinned_task_ids=pinned_task_ids, crew=crew,
        )
    solve_time = time.time() - start

    return _materialize_plan(
        db, horizon, tasks, safe_slots, eligible, assignment, engine_used, solver_status, solve_time, unsafe_excluded,
        effective_duration=effective_duration, weather_exclusion_reason=weather_exclusion_reason,
        override_meta=override_meta, crew=crew, released_pins=released_pins,
    )


def simulate_schedule(
    db: Session,
    horizon: str,
    horizon_start: dt.date = None,
    corridor_capacity: int = DEFAULT_CORRIDOR_CAPACITY,
    exclude_corridors: list = None,
    coordination_bonus: float = None,
    extra_slot_hours: dict = None,
) -> dict:
    """The read-only twin of run_schedule: solves and computes metrics but
    NEVER calls _materialize_plan — no BlockPlan/BlockPlanEntry is written,
    no MaintenanceTask.status is touched. This is what what-if analysis and
    shadow-price probing actually run against, so exploring "what if this
    corridor were closed" or "what if we had 2 more hours Tuesday night"
    can never corrupt the real draft/published plan or task state.

    exclude_corridors: corridor_ids to drop from the candidate pool (the
      "a corridor closed" scenario).
    coordination_bonus: override the objective's cross-department overlap
      bonus for this simulation only (the "different objective weights"
      scenario).
    extra_slot_hours: {corridor_id: extra_hours} — extends every safe slot
      on that corridor by extra_hours (the "extra crew/time available"
      scenario, and exactly what shadow-price probing perturbs).
    """
    if horizon not in HORIZON_DAYS:
        raise ValueError("horizon must be 'weekly' or 'monthly'")
    horizon_start = horizon_start or ist_today()  # "this week"/"today" means IST, not server-local or UTC
    coordination_bonus = COORDINATION_BONUS if coordination_bonus is None else coordination_bonus

    horizon_days = HORIZON_DAYS[horizon]
    tasks = (
        db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.status.in_(["submitted", "unscheduled", "scheduled"]))
        .all()
    )
    safe_slots, unsafe_excluded = get_safe_slots(db, horizon, horizon_start)
    if exclude_corridors:
        safe_slots = [s for s in safe_slots if s.corridor_id not in exclude_corridors]
    if extra_slot_hours:
        for s in safe_slots:
            if s.corridor_id in extra_slot_hours:
                s.end_time = s.end_time + dt.timedelta(hours=extra_slot_hours[s.corridor_id])

    corridor_matched = {t.task_id: [s for s in safe_slots if s.corridor_id == t.corridor_id] for t in tasks}
    weather = weather_service.assess_candidates(db, tasks, corridor_matched, horizon_start, horizon_days)
    eligible = weather["eligible"]
    effective_duration = weather["effective_duration"]

    # epoch is the UTC instant of IST midnight on horizon_start — NOT naive
    # local midnight — so the minute-offset arithmetic below (_minutes/
    # _from_minutes) stays consistent with every naive-but-UTC slot/
    # occurrence time it's compared against (see tz_utils.py).
    epoch, _ = ist_date_to_utc_bounds(horizon_start)
    slot_by_id = {s.slot_id: s for s in safe_slots}
    tasks_by_id = {t.task_id: t for t in tasks}

    crew = crew_capacity.CrewCapacity.load(db)

    start = time.time()
    try:
        assignment, engine_used, solver_status = _solve_cp_sat(
            tasks, safe_slots, eligible, epoch, corridor_capacity, coordination_bonus=coordination_bonus,
            effective_duration=effective_duration, crew=crew,
        )
    except Exception:
        assignment, engine_used, solver_status = _solve_greedy(
            tasks, safe_slots, eligible, epoch, corridor_capacity, effective_duration=effective_duration, crew=crew,
        )
    solve_time = time.time() - start

    scheduled_list = [
        {
            "task_id": task_id,
            "department": tasks_by_id[task_id].department,
            "corridor_id": slot_by_id[slot_id].corridor_id,
            "slot_id": slot_id,
            "start": start_dt,
            "end": end_dt,
            "duration_hours": tasks_by_id[task_id].required_duration_hours,
            "priority_score": tasks_by_id[task_id].priority_score,
            "overdue_days": tasks_by_id[task_id].overdue_days,
        }
        for task_id, (slot_id, start_dt, end_dt) in assignment.items()
    ]
    shared_metrics = metrics_mod.compute_metrics(scheduled_list, tasks, safe_slots, full_window_downtime=False)

    return {
        "metrics": shared_metrics,
        "engine_used": engine_used,
        "solver_status": solver_status,
        "solve_time_seconds": round(solve_time, 3),
        "scheduled_task_ids": sorted(assignment.keys()),
        "unscheduled_task_ids": sorted(t.task_id for t in tasks if t.task_id not in assignment),
        "total_objective_priority_score": round(sum(tasks_by_id[tid].priority_score for tid in assignment), 1),
    }


def _effective_duration_hours(effective_duration, task_id, slot_id, fallback_hours):
    entry = (effective_duration or {}).get((task_id, slot_id))
    return entry["effective_hours"] if entry else fallback_hours


def _add_department_crew_cumulatives(model, tasks, candidate, epoch, min_minute, max_minute, crew):
    """Feature 10: ONE AddCumulative per department over every candidate
    interval of that department on EVERY corridor — gangs are a
    department-wide resource, so this deliberately does not group by
    corridor the way the corridor-capacity cumulative does. Both must hold
    at once: an interval is only allowed where its corridor has physical
    room AND its department has a gang free.

    Each real task demands 1 (one gang). Per-date capacity is folded into
    the same single constraint: the ceiling is the department's HIGHEST
    capacity across the model's time domain, and every IST day whose
    capacity is lower gets a fixed, always-present interval spanning that
    day with demand (ceiling - that day's capacity). On such a day only the
    day's real capacity is left for work, so e.g. SNT=1 on 15 Sep holds
    00:00-24:00 IST on 15 Sep while neighbouring days keep the default.
    Returns {dept: {"ceiling": int, "reserved_days": int}} for diagnostics."""
    intervals_by_dept = {}
    for t in tasks:
        for (_s, _e, _p, interval) in candidate.get(t.task_id, {}).values():
            intervals_by_dept.setdefault(t.department, []).append(interval)

    domain_start = _from_minutes(min_minute, epoch)
    domain_end = _from_minutes(max_minute, epoch)
    dates = crew_capacity.ist_dates_spanned(domain_start, domain_end) if max_minute > min_minute else []

    summary = {}
    for dept, intervals in intervals_by_dept.items():
        if not crew.constrains(dept) or not dates:
            continue
        caps = {d: crew.capacity(dept, d) for d in dates}
        known = [c for c in caps.values() if c is not None]
        if not known:
            continue
        ceiling = max(known)
        all_intervals = list(intervals)
        demands = [1] * len(intervals)
        reserved = 0
        for d, cap in caps.items():
            if cap is None or cap >= ceiling:
                continue
            day_start, day_end = ist_date_to_utc_bounds(d)
            s = max(_minutes(day_start, epoch), min_minute)
            e = min(_minutes(day_end, epoch), max_minute)
            if e <= s:
                continue
            all_intervals.append(model.NewIntervalVar(s, e - s, e, f"crew_reserve_{dept}_{d.isoformat()}"))
            demands.append(ceiling - cap)
            reserved += 1
        model.AddCumulative(all_intervals, demands, ceiling)
        summary[dept] = {"ceiling": ceiling, "reserved_days": reserved}
    return summary


def _solve_cp_sat(tasks, slots, eligible, epoch, corridor_capacity, previous_assignment_min=None, stability_bonus=0.0, coordination_bonus=None, effective_duration=None, pinned_task_ids=None, crew=None):
    if _cp_model is None:
        raise RuntimeError("OR-Tools is not available in this environment")
    cp_model = _cp_model
    pinned_task_ids = pinned_task_ids or set()

    tasks_by_id = {t.task_id: t for t in tasks}
    slot_by_id = {s.slot_id: s for s in slots}

    # Domain bounds wide enough to cover every safe slot, even a manually
    # entered one that happens to sit outside the nominal horizon window.
    all_bounds = [_minutes(s.start_time, epoch) for s in slots] + [_minutes(s.end_time, epoch) for s in slots] + [0]
    min_minute, max_minute = min(all_bounds), max(all_bounds)

    model = cp_model.CpModel()

    # candidate[task_id][slot_id] = (start_var, end_var, presence_lit, interval_var)
    candidate = {}
    for t in tasks:
        for s in eligible[t.task_id]:
            # Duration is PER-(task, window): a weather soft-buffer rule can
            # inflate the effective duration differently in different
            # candidate windows (each window may carry a different day's
            # forecast) — see weather_service.assess_candidates. Falls back
            # to the task's own base duration when no buffer applies.
            duration_min = int(round(_effective_duration_hours(effective_duration, t.task_id, s.slot_id, t.required_duration_hours) * 60))
            w_start = _minutes(s.start_time, epoch)
            w_end = _minutes(s.end_time, epoch)
            latest_start = w_end - duration_min
            if latest_start < w_start:
                continue
            start_var = model.NewIntVar(w_start, latest_start, f"start_{t.task_id}_{s.slot_id}")
            end_var = model.NewIntVar(w_start + duration_min, w_end, f"end_{t.task_id}_{s.slot_id}")
            presence = model.NewBoolVar(f"present_{t.task_id}_{s.slot_id}")
            interval = model.NewOptionalIntervalVar(start_var, duration_min, end_var, presence, f"iv_{t.task_id}_{s.slot_id}")
            if t.task_id in pinned_task_ids:
                # A manual override being preserved across this re-run: force
                # the solver to keep it exactly here rather than treating it
                # as just another optional candidate it might drop or move.
                model.Add(presence == 1)
            candidate.setdefault(t.task_id, {})[s.slot_id] = (start_var, end_var, presence, interval)

    # each task placed in at most one candidate window; scheduled[t] tracks whether it was placed at all
    scheduled = {}
    eff_start = {}
    eff_end = {}
    for t in tasks:
        cands = candidate.get(t.task_id, {})
        sched_var = model.NewBoolVar(f"scheduled_{t.task_id}")
        scheduled[t.task_id] = sched_var
        if not cands:
            model.Add(sched_var == 0)
            continue
        presences = [c[2] for c in cands.values()]
        model.Add(sum(presences) == sched_var)

        es = model.NewIntVar(min_minute, max_minute, f"eff_start_{t.task_id}")
        ee = model.NewIntVar(min_minute, max_minute, f"eff_end_{t.task_id}")
        eff_start[t.task_id] = es
        eff_end[t.task_id] = ee
        for slot_id, (start_var, end_var, presence, _interval) in cands.items():
            model.Add(es == start_var).OnlyEnforceIf(presence)
            model.Add(ee == end_var).OnlyEnforceIf(presence)

    # per-corridor capacity: up to N concurrent work parties may genuinely overlap
    intervals_by_corridor = {}
    for t in tasks:
        for slot_id, (_s, _e, _p, interval) in candidate.get(t.task_id, {}).items():
            corridor_id = slot_by_id[slot_id].corridor_id
            intervals_by_corridor.setdefault(corridor_id, []).append(interval)
    for corridor_id, intervals in intervals_by_corridor.items():
        model.AddCumulative(intervals, [1] * len(intervals), corridor_capacity)

    # per-department crew capacity: HARD, spans all corridors (Feature 10)
    if crew is not None:
        _add_department_crew_cumulatives(model, tasks, candidate, epoch, min_minute, max_minute, crew)

    # mutual exclusion: the two tasks' intervals (across ALL of either one's
    # candidate windows) may never overlap in time, wherever they land
    seen_pairs = set()
    for t in tasks:
        others = [o.strip() for o in (t.mutually_exclusive_with or "").split(",") if o.strip()]
        for other_id in others:
            if other_id not in tasks_by_id:
                continue
            pair = tuple(sorted((t.task_id, other_id)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            pooled = [iv for (_s, _e, _p, iv) in candidate.get(pair[0], {}).values()]
            pooled += [iv for (_s, _e, _p, iv) in candidate.get(pair[1], {}).values()]
            if len(pooled) >= 2:
                model.AddNoOverlap(pooled)

    # coordination: reward genuine temporal overlap between different-department
    # tasks sharing a corridor. One-directional reification is sufficient since
    # this is purely an objective bonus — the solver can never claim a bonus for
    # a pair that isn't both scheduled and actually overlapping in time.
    overlap_terms = []
    by_corridor = {}
    for t in tasks:
        if t.task_id in scheduled:
            by_corridor.setdefault(t.corridor_id, []).append(t)
    for corridor_id, corridor_tasks in by_corridor.items():
        for i in range(len(corridor_tasks)):
            for j in range(i + 1, len(corridor_tasks)):
                t1, t2 = corridor_tasks[i], corridor_tasks[j]
                if t1.department == t2.department:
                    continue
                if t1.task_id not in eff_start or t2.task_id not in eff_start:
                    continue
                overlap = model.NewBoolVar(f"overlap_{t1.task_id}_{t2.task_id}")
                model.Add(scheduled[t1.task_id] == 1).OnlyEnforceIf(overlap)
                model.Add(scheduled[t2.task_id] == 1).OnlyEnforceIf(overlap)
                model.Add(eff_start[t1.task_id] < eff_end[t2.task_id]).OnlyEnforceIf(overlap)
                model.Add(eff_start[t2.task_id] < eff_end[t1.task_id]).OnlyEnforceIf(overlap)
                overlap_terms.append(overlap)

    # deviation penalty (emergency re-optimization): reward keeping a task
    # exactly where the previously-published plan had it. Reification is
    # one-directional exactly like the coordination bonus above — "unchanged"
    # can only be set to 1 by the solver when the task is genuinely still
    # scheduled at exactly its previous start time, so the bonus can never be
    # claimed falsely; the solver is simply never forced to claim it either.
    stability_terms = []
    if previous_assignment_min and stability_bonus > 0:
        for t in tasks:
            if t.task_id not in previous_assignment_min or t.task_id not in eff_start:
                continue
            prev_start_min, _prev_end_min = previous_assignment_min[t.task_id]
            unchanged = model.NewBoolVar(f"unchanged_{t.task_id}")
            model.Add(scheduled[t.task_id] == 1).OnlyEnforceIf(unchanged)
            model.Add(eff_start[t.task_id] == prev_start_min).OnlyEnforceIf(unchanged)
            stability_terms.append(unchanged)

    effective_coordination_bonus = COORDINATION_BONUS if coordination_bonus is None else coordination_bonus
    objective_terms = []
    for t in tasks:
        score_int = int(round(t.priority_score * SCALE))
        objective_terms.append(score_int * scheduled[t.task_id])
    for overlap in overlap_terms:
        objective_terms.append(int(effective_coordination_bonus * SCALE) * overlap)
    for unchanged in stability_terms:
        objective_terms.append(int(stability_bonus * SCALE) * unchanged)

    model.Maximize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVE_TIME_LIMIT_SECONDS
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"CP-SAT returned no usable solution (status={solver.StatusName(status)})")

    assignment = {}
    for t in tasks:
        cands = candidate.get(t.task_id, {})
        for slot_id, (start_var, end_var, presence, _interval) in cands.items():
            if solver.Value(presence) == 1:
                start_dt = _from_minutes(solver.Value(start_var), epoch)
                end_dt = _from_minutes(solver.Value(end_var), epoch)
                assignment[t.task_id] = (slot_id, start_dt, end_dt)
                break

    engine_used = "cp_sat_optimal" if status == cp_model.OPTIMAL else "cp_sat_feasible"
    return assignment, engine_used, solver.StatusName(status)


def _solve_greedy(tasks, slots, eligible, epoch, corridor_capacity, effective_duration=None, pinned_task_ids=None, crew=None):
    """Deterministic fallback: highest priority first, actual interval
    placement (not whole-window assignment). For each task, tries its
    eligible windows (preferring ones that already host a different
    department, to actively chase coordination) and, within a window,
    scans candidate start times — window start plus every existing
    interval's end time that could still fit — picking the earliest one
    that both respects the corridor's concurrency capacity and doesn't
    overlap any mutually-exclusive partner already placed in any window.
    Never hard-fails; a task simply stays unscheduled if no window has room.

    `eligible` has already had weather-hard-excluded windows removed (see
    weather_service.assess_candidates), so the greedy fallback respects the
    same safety exclusion as CP-SAT with no extra logic here — it simply
    never sees an unsafe candidate. `effective_duration` supplies the
    per-(task, window) weather-buffered duration, same as CP-SAT.

    pinned_task_ids: tasks whose `eligible` list has already been narrowed
    (see _apply_manual_pins) to exactly their preserved manual-override
    window. They're seeded into `placed` FIRST, before priority ordering
    even runs, so every other task's capacity/exclusion search sees them as
    already-occupied — a pin can never be silently bumped by a
    higher-priority free task discovered later in the loop.

    crew (Feature 10): tracks each department's concurrently-placed work
    across ALL corridors and skips any start time at which the department
    has no gang left (crew_capacity.crew_conflict — the identical count
    check the manual paths use). Candidate start times additionally include
    the end of every same-department task on ANY corridor and every IST
    midnight, since either can be the moment a gang frees up."""
    pinned_task_ids = pinned_task_ids or set()
    tasks_by_id = {t.task_id: t for t in tasks}
    placed = []  # list of dicts: task_id, slot_id, corridor_id, start_min, end_min, department
    assignment = {}

    for t in tasks:
        if t.task_id not in pinned_task_ids:
            continue
        windows = eligible.get(t.task_id) or []
        if not windows:
            continue
        w = windows[0]
        start_min = _minutes(w.start_time, epoch)
        end_min = _minutes(w.end_time, epoch)
        placed.append({
            "task_id": t.task_id, "slot_id": w.slot_id, "corridor_id": w.corridor_id,
            "start_min": start_min, "end_min": end_min, "department": t.department,
        })
        assignment[t.task_id] = (w.slot_id, w.start_time, w.end_time)

    def excluded_ids(t):
        return set(x.strip() for x in (t.mutually_exclusive_with or "").split(",") if x.strip())

    ordered = sorted((t for t in tasks if t.task_id not in pinned_task_ids), key=lambda t: t.priority_score, reverse=True)

    for t in ordered:
        candidates = eligible[t.task_id]
        if not candidates:
            continue

        excl = excluded_ids(t)
        excl_partners_global = [
            p for p in placed
            if p["task_id"] in excl or t.task_id in excluded_ids(tasks_by_id[p["task_id"]])
        ]

        def coordination_value(w):
            depts = {p["department"] for p in placed if p["slot_id"] == w.slot_id}
            return 1 if (depts and t.department not in depts) else 0

        sorted_windows = sorted(candidates, key=lambda w: (-coordination_value(w), w.start_time))

        placed_ok = False
        for w in sorted_windows:
            duration_min = int(round(_effective_duration_hours(effective_duration, t.task_id, w.slot_id, t.required_duration_hours) * 60))
            w_start_min = _minutes(w.start_time, epoch)
            w_end_min = _minutes(w.end_time, epoch)
            latest_start = w_end_min - duration_min
            if latest_start < w_start_min:
                continue

            same_corridor_same_window = [p for p in placed if p["slot_id"] == w.slot_id]
            relevant_excl = [p for p in excl_partners_global]
            same_dept = [p for p in placed if p["department"] == t.department] if crew is not None else []
            dept_committed = [(_from_minutes(p["start_min"], epoch), _from_minutes(p["end_min"], epoch)) for p in same_dept]

            breakpoints = {w_start_min}
            for p in same_corridor_same_window + relevant_excl + same_dept:
                if w_start_min <= p["end_min"] <= latest_start:
                    breakpoints.add(p["end_min"])
            if crew is not None:
                for midnight in crew_capacity.ist_midnights_between(
                    _from_minutes(w_start_min, epoch), _from_minutes(latest_start, epoch) + dt.timedelta(seconds=1)
                ):
                    breakpoints.add(_minutes(midnight, epoch))
            for cand_start in sorted(breakpoints):
                cand_end = cand_start + duration_min
                overlap_count = sum(
                    1 for p in same_corridor_same_window if p["start_min"] < cand_end and cand_start < p["end_min"]
                )
                if overlap_count + 1 > corridor_capacity:
                    continue
                conflict = any(p["start_min"] < cand_end and cand_start < p["end_min"] for p in relevant_excl)
                if conflict:
                    continue
                if crew is not None and crew_capacity.crew_conflict(
                    dept_committed, _from_minutes(cand_start, epoch), _from_minutes(cand_end, epoch), t.department, crew
                ):
                    continue

                placed.append(
                    {
                        "task_id": t.task_id,
                        "slot_id": w.slot_id,
                        "corridor_id": w.corridor_id,
                        "start_min": cand_start,
                        "end_min": cand_end,
                        "department": t.department,
                    }
                )
                assignment[t.task_id] = (w.slot_id, _from_minutes(cand_start, epoch), _from_minutes(cand_end, epoch))
                placed_ok = True
                break
            if placed_ok:
                break

    return assignment, "greedy_fallback", "GREEDY"


def _materialize_plan(db, horizon, tasks, slots, eligible, assignment, engine_used, solver_status, solve_time, unsafe_excluded,
                       effective_duration=None, weather_exclusion_reason=None, override_meta=None, crew=None,
                       released_pins=None):
    slot_by_id = {s.slot_id: s for s in slots}
    tasks_by_id = {t.task_id: t for t in tasks}
    effective_duration = effective_duration or {}
    weather_exclusion_reason = weather_exclusion_reason or {}
    override_meta = override_meta or {}

    # supersede prior drafts for this horizon
    prior_drafts = db.query(models.BlockPlan).filter_by(horizon=horizon, status="draft").all()
    for p in prior_drafts:
        p.status = "superseded"

    max_version = db.query(models.BlockPlan).filter_by(horizon=horizon).count()
    plan_id = f"plan-{horizon}-{uuid.uuid4().hex[:8]}"
    plan = models.BlockPlan(plan_id=plan_id, horizon=horizon, version=max_version + 1, status="draft")
    db.add(plan)

    # co-scheduling is now determined by genuine temporal overlap, not "same
    # window" — recomputed directly from the actual solved start/end times so
    # it's authoritative regardless of which engine produced the assignment.
    # duration_hours is the WEATHER-EFFECTIVE duration actually used to build
    # the interval (== end - start whenever a soft buffer applied), so
    # metrics/utilization reflect real, weather-adjusted work time rather
    # than silently disagreeing with the assigned window span.
    scheduled_list = []
    for task_id, (slot_id, start_dt, end_dt) in assignment.items():
        task = tasks_by_id[task_id]
        wx = effective_duration.get((task_id, slot_id))
        scheduled_list.append(
            {
                "task_id": task_id,
                "department": task.department,
                "corridor_id": slot_by_id[slot_id].corridor_id,
                "slot_id": slot_id,
                "start": start_dt,
                "end": end_dt,
                "duration_hours": wx["effective_hours"] if wx else task.required_duration_hours,
                "priority_score": task.priority_score,
                "overdue_days": task.overdue_days,
            }
        )

    co_scheduled = {e["task_id"]: set() for e in scheduled_list}
    for i in range(len(scheduled_list)):
        for j in range(i + 1, len(scheduled_list)):
            a, b = scheduled_list[i], scheduled_list[j]
            if a["corridor_id"] != b["corridor_id"] or a["department"] == b["department"]:
                continue
            if a["start"] < b["end"] and b["start"] < a["end"]:
                co_scheduled[a["task_id"]].add(b["department"])
                co_scheduled[b["task_id"]].add(a["department"])

    scheduled_ids = set()
    total_score_scheduled = 0.0
    for e in scheduled_list:
        task = tasks_by_id[e["task_id"]]
        meta = override_meta.get(e["task_id"])
        entry = models.BlockPlanEntry(
            plan_id=plan_id,
            task_id=e["task_id"],
            slot_id=e["slot_id"],
            corridor_id=e["corridor_id"],
            department=e["department"],
            assigned_window_start=e["start"],
            assigned_window_end=e["end"],
            co_scheduled_departments=",".join(sorted(co_scheduled[e["task_id"]])),
            # Carries a pinned manual override's provenance FORWARD into this
            # new plan's entry (see _apply_manual_pins) — a re-run must not
            # quietly turn a manually-placed block back into an
            # optimizer-looking one just because a new BlockPlanEntry row
            # was created for it.
            override=bool(meta),
            override_reason=(meta or {}).get("override_reason", ""),
            override_by=(meta or {}).get("override_by", ""),
            override_at=(meta or {}).get("override_at"),
        )
        db.add(entry)
        task.status = "scheduled"
        task.unscheduled_reason = ""
        scheduled_ids.add(e["task_id"])
        total_score_scheduled += task.priority_score

        # Weather audit trail: only written when a soft buffer actually
        # changed the duration used, so the table stays a record of
        # weather-INFLUENCED outcomes, not a row per task.
        wx = effective_duration.get((e["task_id"], e["slot_id"]))
        if wx and wx["buffer_pct"] > 0:
            db.add(
                models.TaskWeatherAssessment(
                    task_id=e["task_id"], plan_id=plan_id, horizon=horizon, corridor_id=e["corridor_id"],
                    slot_id=e["slot_id"], outcome="soft_buffered",
                    base_duration_hours=wx["base_hours"], effective_duration_hours=wx["effective_hours"],
                    buffer_pct_applied=wx["buffer_pct"], reason=wx["note"],
                )
            )

    # Gang labels (Feature 10): post-solve greedy colouring, not part of the
    # model — see crew_capacity.assign_gangs.
    gang_by_task = crew_capacity.write_gang_assignments(db, plan_id, scheduled_list)

    # Department work actually committed in this plan, across all corridors
    # — what the crew-capacity reason below is judged against.
    committed_by_dept = {}
    for e in scheduled_list:
        committed_by_dept.setdefault(e["department"], []).append((e["start"], e["end"]))

    unscheduled = []
    crew_limited = {}
    for t in tasks:
        if t.task_id in scheduled_ids:
            continue
        eligible_exists = len(eligible.get(t.task_id, [])) > 0
        weather_reason = weather_exclusion_reason.get(t.task_id)
        reason = _build_reason(t, eligible_exists, weather_reason)
        if not eligible_exists:
            reason_code = "weather" if weather_reason else "no_corridor_slot"
        else:
            reason_code = "competition"
        if eligible_exists and crew is not None:
            windows = []
            for s in eligible[t.task_id]:
                hours = _effective_duration_hours(effective_duration, t.task_id, s.slot_id, t.required_duration_hours)
                duration = dt.timedelta(minutes=int(round(hours * 60)))
                if s.end_time - s.start_time >= duration:
                    windows.append((s, duration))
            crew_reason = crew_capacity.crew_unscheduled_reason(t, windows, crew, committed_by_dept.get(t.department, []))
            if crew_reason:
                reason = crew_reason
                reason_code = crew_capacity.CREW_REASON_CODE
                crew_limited.setdefault(t.department, []).append(t.task_id)
        t.status = "unscheduled"
        t.unscheduled_reason = reason
        unscheduled.append({
            "task_id": t.task_id, "department": t.department, "corridor_id": t.corridor_id, "reason": reason,
            "reason_code": reason_code,
        })

        if weather_reason:
            db.add(
                models.TaskWeatherAssessment(
                    task_id=t.task_id, plan_id=plan_id, horizon=horizon, corridor_id=t.corridor_id,
                    outcome="hard_excluded", base_duration_hours=t.required_duration_hours,
                    effective_duration_hours=t.required_duration_hours, reason=weather_reason,
                )
            )

    shared_metrics = metrics_mod.compute_metrics(scheduled_list, tasks, slots, full_window_downtime=False)

    metrics = {
        "horizon": horizon,
        # scheduled_count/total_priority_score_scheduled are the original key
        # names the frontend's metrics strip already reads; tasks_completed/
        # priority_weighted_completion are the shared-metrics names used by
        # baseline.py and the /schedule/comparison endpoint — both are kept
        # in sync here (same values, two names) so a plan's stored metrics
        # are a strict superset comparable against the baseline's.
        "scheduled_count": len(scheduled_ids),
        "tasks_completed": len(scheduled_ids),
        "unscheduled_count": len(unscheduled),
        "coordinated_blocks": shared_metrics["coordinated_blocks"],
        "total_priority_score_scheduled": round(total_score_scheduled, 1),
        "priority_weighted_completion": round(total_score_scheduled, 1),
        "solve_time_seconds": round(solve_time, 3),
        "engine_used": engine_used,
        "solver_status": solver_status,
        "unsafe_slots_excluded": unsafe_excluded,
        "tasks_total": shared_metrics["tasks_total"],
        "total_corridor_closures": shared_metrics["total_corridor_closures"],
        "total_downtime_hours": shared_metrics["total_downtime_hours"],
        "block_utilization_pct": shared_metrics["block_utilization_pct"],
        "avg_overdue_days_of_scheduled_tasks": shared_metrics["avg_overdue_days_of_scheduled_tasks"],
        # Feature 10 — crew capacity. crew_limited_unscheduled is keyed by
        # department so the resource strip can tell "left out for want of a
        # gang" (a crew problem) apart from corridor/competition losses.
        "crew_capacity_enforced": crew is not None,
        "crew_limited_unscheduled": crew_limited,
        "crew_limited_unscheduled_count": sum(len(v) for v in crew_limited.values()),
        "gangs_used": {
            dept: len({g for tid, g in gang_by_task.items() if tasks_by_id[tid].department == dept})
            for dept in sorted({tasks_by_id[tid].department for tid in gang_by_task})
        },
        "pinned_overrides_released_for_crew_capacity": released_pins or [],
    }
    plan.metrics_json = json.dumps(metrics)

    db.commit()

    return {
        "plan_id": plan_id, "version": plan.version, "metrics": metrics, "unscheduled": unscheduled,
        "gang_assignments": [
            {"task_id": e["task_id"], "department": e["department"], "gang_id": gang_by_task[e["task_id"]],
             "corridor_id": e["corridor_id"], "start": e["start"], "end": e["end"]}
            for e in sorted(scheduled_list, key=lambda e: (e["department"], e["start"]))
        ],
    }


def _recompute_plan_co_scheduled_and_metrics(db: Session, plan) -> dict:
    """Shared by reschedule_entry (and reusable by any future manual-edit
    path): rebuilds co_scheduled_departments for every entry in a plan from
    scratch (genuine pairwise time-overlap, same rule _materialize_plan
    uses) and recomputes the plan's stored metrics against the current
    entry set. Returns the fresh metrics dict."""
    entries = db.query(models.BlockPlanEntry).filter_by(plan_id=plan.plan_id).all()
    tasks_by_id = {
        t.task_id: t
        for t in db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.task_id.in_([e.task_id for e in entries]))
        .all()
    }

    co_scheduled = {e.id: set() for e in entries}
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            if a.corridor_id != b.corridor_id or a.department == b.department:
                continue
            if a.assigned_window_start < b.assigned_window_end and b.assigned_window_start < a.assigned_window_end:
                co_scheduled[a.id].add(b.department)
                co_scheduled[b.id].add(a.department)
    for e in entries:
        e.co_scheduled_departments = ",".join(sorted(co_scheduled[e.id]))

    # A manual edit changes who is working when — relabel gangs for the
    # plan's CURRENT entries so gang sheets never describe a stale plan.
    crew_capacity.write_gang_assignments(db, plan.plan_id, [
        {"task_id": e.task_id, "department": e.department, "corridor_id": e.corridor_id,
         "start": e.assigned_window_start, "end": e.assigned_window_end}
        for e in entries
    ])

    scheduled_list = [
        {
            "task_id": e.task_id,
            "department": e.department,
            "corridor_id": e.corridor_id,
            "slot_id": e.slot_id,
            "start": e.assigned_window_start,
            "end": e.assigned_window_end,
            "duration_hours": tasks_by_id[e.task_id].required_duration_hours,
            "priority_score": tasks_by_id[e.task_id].priority_score,
            "overdue_days": tasks_by_id[e.task_id].overdue_days,
        }
        for e in entries
        if e.task_id in tasks_by_id
    ]
    all_tasks = db.query(models.MaintenanceTask).all()
    safe_slots, _unsafe = get_safe_slots(db, plan.horizon)
    shared_metrics = metrics_mod.compute_metrics(scheduled_list, all_tasks, safe_slots, full_window_downtime=False)

    old_metrics = json.loads(plan.metrics_json or "{}")
    old_metrics.update(
        {
            "coordinated_blocks": shared_metrics["coordinated_blocks"],
            "total_corridor_closures": shared_metrics["total_corridor_closures"],
            "total_downtime_hours": shared_metrics["total_downtime_hours"],
            "block_utilization_pct": shared_metrics["block_utilization_pct"],
            "avg_overdue_days_of_scheduled_tasks": shared_metrics["avg_overdue_days_of_scheduled_tasks"],
        }
    )
    plan.metrics_json = json.dumps(old_metrics)
    return old_metrics


def reschedule_entry(db: Session, entry_id: int, new_start: dt.datetime, corridor_capacity: int = DEFAULT_CORRIDOR_CAPACITY) -> dict:
    """Manually moves one BlockPlanEntry to a new start time, re-validating
    every constraint the optimizer itself enforces — this is what makes
    FR-COA-03's "manually adjust any proposed block allocation" real rather
    than a rename of approve-or-reject. Raises ValueError with a specific,
    human-readable reason for any rejected move; the caller (the router) is
    expected to turn that into a 400 response verbatim. Only draft plans can
    be adjusted — once published, a plan is frozen (governance: publishing
    is the one, explicit, human-approved commitment)."""
    entry = db.query(models.BlockPlanEntry).filter_by(id=entry_id).first()
    if not entry:
        raise ValueError(f"plan entry {entry_id} not found")

    plan = db.query(models.BlockPlan).filter_by(plan_id=entry.plan_id).first()
    if not plan or plan.status != "draft":
        raise ValueError("only a draft plan's blocks can be manually adjusted — this plan is "
                          f"'{plan.status if plan else 'missing'}'")

    task = db.query(models.MaintenanceTask).filter_by(task_id=entry.task_id).first()
    if not task:
        raise ValueError(f"task {entry.task_id} not found")

    new_end = new_start + dt.timedelta(hours=task.required_duration_hours)

    # 1) real-train safety: check the actual two calendar days the new
    # window touches, independent of whatever horizon_start the original
    # run happened to use (which isn't persisted with the plan). Use the
    # IST calendar date, not new_start.date() (a naive-but-UTC value) —
    # near IST midnight those two dates can differ, and corridor_occurrences
    # expects an IST calendar date (see its docstring).
    occurrences = timetable_loader.corridor_occurrences(db, entry.corridor_id, to_ist(new_start).date(), 2)
    for occ in occurrences:
        if occ["start"] < new_end and new_start < occ["end"]:
            raise ValueError(
                f"would overlap real train {occ['train_id']} "
                f"({occ['start'].strftime('%H:%M')}–{occ['end'].strftime('%H:%M')}) on {entry.corridor_id}"
            )

    # 2) must still fit fully inside SOME available corridor window
    candidate_slots = (
        db.query(models.CorridorSlot)
        .filter(models.CorridorSlot.corridor_id == entry.corridor_id, models.CorridorSlot.horizon == plan.horizon)
        .all()
    )
    if not any(new_start >= s.start_time and new_end <= s.end_time for s in candidate_slots):
        raise ValueError(
            f"{new_start.strftime('%Y-%m-%d %H:%M')}–{new_end.strftime('%H:%M')} does not fit fully inside "
            f"any available corridor window for {entry.corridor_id}"
        )

    # 3) corridor capacity: how many OTHER entries on this corridor, in this
    # plan, would genuinely overlap the new window
    siblings = (
        db.query(models.BlockPlanEntry)
        .filter(
            models.BlockPlanEntry.plan_id == plan.plan_id,
            models.BlockPlanEntry.corridor_id == entry.corridor_id,
            models.BlockPlanEntry.id != entry.id,
        )
        .all()
    )
    overlapping = [s for s in siblings if s.assigned_window_start < new_end and new_start < s.assigned_window_end]
    if len(overlapping) + 1 > corridor_capacity:
        raise ValueError(
            f"would exceed corridor capacity ({corridor_capacity}) on {entry.corridor_id} — "
            f"{len(overlapping)} other task(s) already occupy that time"
        )

    # 4) mutual exclusion against every other task in this plan, any corridor
    excluded_ids = set(x.strip() for x in (task.mutually_exclusive_with or "").split(",") if x.strip())
    if excluded_ids:
        plan_entries = db.query(models.BlockPlanEntry).filter(models.BlockPlanEntry.plan_id == plan.plan_id).all()
        for other in plan_entries:
            if other.task_id in excluded_ids and other.assigned_window_start < new_end and new_start < other.assigned_window_end:
                raise ValueError(f"would overlap mutually-exclusive task {other.task_id}")

    # 5) department crew capacity, across every corridor (Feature 10)
    crew_violation = crew_capacity.manual_placement_violation(db, plan.plan_id, task, new_start, new_end, exclude_entry_ids=[entry.id])
    if crew_violation:
        raise ValueError(crew_violation)

    entry.assigned_window_start = new_start
    entry.assigned_window_end = new_end
    metrics = _recompute_plan_co_scheduled_and_metrics(db, plan)
    db.commit()
    db.refresh(entry)

    return {
        "id": entry.id,
        "task_id": entry.task_id,
        "corridor_id": entry.corridor_id,
        "department": entry.department,
        "assigned_window_start": entry.assigned_window_start,
        "assigned_window_end": entry.assigned_window_end,
        "co_scheduled_departments": [d for d in entry.co_scheduled_departments.split(",") if d],
        "metrics": metrics,
    }


# ================================================================
# Manual overrides (FR-COA-03): COA/ADMIN can place, remove, or swap a
# specific task into/out of a specific corridor window, bypassing the
# optimizer's own decisions entirely. Unlike reschedule_entry() above —
# which re-validates every constraint the optimizer itself enforces and
# rejects ANY violation — these three re-validate only the HARD SAFETY
# constraints (real-train overlap, window containment, corridor match).
# Everything else the optimizer would have preferred (corridor concurrency,
# mutual exclusion, weather, competing priority) is reported back as a
# WARNING but never blocks the override; that distinction between "cannot
# be done" and "the optimizer wouldn't have done this, but you can" is the
# entire point of a manual override, not an oversight.
# ================================================================

def _hard_safety_check(db: Session, task: models.MaintenanceTask, slot: models.CorridorSlot, start: dt.datetime, end: dt.datetime) -> None:
    """The three HARD constraints a manual override can never bypass. Raises
    ValueError with a specific, human-readable reason for the first one
    violated — the caller (the router) turns that into a 400 detail
    verbatim, exactly like reschedule_entry's existing convention."""
    if slot.corridor_id != task.corridor_id:
        raise ValueError(
            f"task {task.task_id}'s corridor ({task.corridor_id}) does not match slot {slot.slot_id}'s corridor ({slot.corridor_id})"
        )
    if start < slot.start_time or end > slot.end_time:
        raise ValueError(
            f"{start.strftime('%Y-%m-%d %H:%M')}-{end.strftime('%H:%M')} does not fit fully inside "
            f"slot {slot.slot_id}'s window ({slot.start_time.strftime('%Y-%m-%d %H:%M')}-{slot.end_time.strftime('%H:%M')})"
        )
    # IST calendar date, not a bare .date() on a naive-but-UTC value — see
    # reschedule_entry's identical comment for why that distinction matters
    # near IST midnight.
    occurrences = timetable_loader.corridor_occurrences(db, slot.corridor_id, to_ist(start).date(), 2)
    for occ in occurrences:
        if occ["start"] < end and start < occ["end"]:
            raise ValueError(
                f"would overlap real train {occ['train_id']} "
                f"({occ['start'].strftime('%H:%M')}-{occ['end'].strftime('%H:%M')}) on {slot.corridor_id}"
            )


def _weather_warnings_for_window(db: Session, task: models.MaintenanceTask, corridor_id: str, start: dt.datetime, end: dt.datetime) -> list:
    """Every admin-configured weather rule (hard, soft, OR priority mode —
    mode doesn't matter here, since for a MANUAL override every weather
    concern is advisory) matching this task's defect_type that the proposed
    window's forecast actually trips. Prefers the hourly forecast for
    exactly this window, same as weather_service.assess_candidates, falling
    back to the daily roll-up when no hourly data has been ingested."""
    rows = weather_service.hourly_rows_for_window(db, corridor_id, start, end)
    fc = weather_service._window_forecast(rows, start, end)
    if fc is None:
        daily = weather_service.get_forecast_rows(db, [corridor_id], to_ist(start).date(), to_ist(end).date())
        fc = daily.get((corridor_id, to_ist(start).date()))
    if fc is None:
        return []
    rules = [r for r in weather_service.get_rules(db) if weather_service._matches(r, task.defect_type)]
    warnings = []
    for rule in rules:
        triggered, detail = weather_service._hazard_triggered(rule, fc)
        if triggered:
            what = "exclude this window entirely" if rule.mode == "hard" else (
                "apply a duration buffer" if rule.mode == "soft" else "raise this task's priority"
            )
            warnings.append(
                f"weather: {rule.label or (rule.hazard + ' risk')} for this window on {corridor_id} ({detail}) "
                f"— the optimizer would normally {what} for this task type"
            )
    return warnings


def _soft_conflicts(db: Session, plan: models.BlockPlan, task: models.MaintenanceTask, start: dt.datetime, end: dt.datetime, exclude_entry_id: int = None) -> tuple:
    """SOFT constraints the optimizer would have preferred to respect but a
    manual override may deliberately bypass. Returns (warnings: list[str],
    hard_ceiling_hit: bool, overlapping_siblings: list[BlockPlanEntry]) —
    hard_ceiling_hit is the ONE case manual_assign_task/manual_swap_tasks
    still refuse outright (see MANUAL_OVERRIDE_HARD_CONCURRENCY_CEILING),
    because beyond it there is no room without silently displacing someone,
    and this codebase never displaces a task without the caller having
    explicitly named it (see manual_swap_tasks)."""
    warnings = []
    corridor_id = task.corridor_id

    siblings_q = db.query(models.BlockPlanEntry).filter(
        models.BlockPlanEntry.plan_id == plan.plan_id,
        models.BlockPlanEntry.corridor_id == corridor_id,
    )
    if exclude_entry_id is not None:
        siblings_q = siblings_q.filter(models.BlockPlanEntry.id != exclude_entry_id)
    siblings = siblings_q.all()
    overlapping = [s for s in siblings if s.assigned_window_start < end and start < s.assigned_window_end]

    ceiling_hit = (len(overlapping) + 1) > MANUAL_OVERRIDE_HARD_CONCURRENCY_CEILING
    if not ceiling_hit and (len(overlapping) + 1) > DEFAULT_CORRIDOR_CAPACITY:
        warnings.append(
            f"corridor {corridor_id} window is already at the optimizer's preferred capacity "
            f"({DEFAULT_CORRIDOR_CAPACITY} concurrent) — this override brings it to {len(overlapping) + 1}"
        )

    excluded_ids = set(x.strip() for x in (task.mutually_exclusive_with or "").split(",") if x.strip())
    if excluded_ids:
        plan_entries = db.query(models.BlockPlanEntry).filter(models.BlockPlanEntry.plan_id == plan.plan_id).all()
        for other in plan_entries:
            if exclude_entry_id is not None and other.id == exclude_entry_id:
                continue
            if other.task_id in excluded_ids and other.assigned_window_start < end and start < other.assigned_window_end:
                warnings.append(f"mutually-exclusive task {other.task_id} already occupies an overlapping window")

    warnings.extend(_weather_warnings_for_window(db, task, corridor_id, start, end))

    competitors = (
        db.query(models.MaintenanceTask)
        .filter(
            models.MaintenanceTask.status == "unscheduled",
            models.MaintenanceTask.corridor_id == corridor_id,
            models.MaintenanceTask.priority_score > task.priority_score,
            models.MaintenanceTask.task_id != task.task_id,
        )
        .order_by(models.MaintenanceTask.priority_score.desc())
        .limit(3)
        .all()
    )
    if competitors:
        names = ", ".join(f"{c.task_id} ({c.priority_score:.1f})" for c in competitors)
        warnings.append(
            f"{len(competitors)} unscheduled task(s) on {corridor_id} have a HIGHER priority score than "
            f"{task.task_id} ({task.priority_score:.1f}) and are competing for this corridor: {names}"
        )

    return warnings, ceiling_hit, overlapping


def _load_active_plan_for_override(db: Session, plan_id: str) -> models.BlockPlan:
    plan = db.query(models.BlockPlan).filter_by(plan_id=plan_id).first()
    if not plan:
        raise ValueError(f"plan {plan_id} not found")
    if plan.status not in ("draft", "published"):
        raise ValueError(
            f"plan is '{plan.status}' — manual overrides can only be made on a draft or currently published plan"
        )
    return plan


def manual_assign_task(db: Session, plan_id: str, task_id: str, slot_id: str, start_time: dt.datetime, user_id: str, reason: str) -> dict:
    """Places `task_id` into `slot_id` at `start_time`, bypassing the
    optimizer entirely. Allowed on a draft OR a published plan (overriding
    a published plan does not require re-approval — the override itself IS
    the COA's decision — but flags the plan as modified_after_publication).
    Raises ValueError with a specific reason for a HARD safety violation;
    soft-constraint violations are returned as `warnings` and never block."""
    plan = _load_active_plan_for_override(db, plan_id)

    task = db.query(models.MaintenanceTask).filter_by(task_id=task_id).first()
    if not task:
        raise ValueError(f"task {task_id} not found")

    if db.query(models.BlockPlanEntry).filter_by(plan_id=plan_id, task_id=task_id).first():
        raise ValueError(f"task {task_id} already has an entry in plan {plan_id} — unschedule it first, or use manual-swap")

    slot = db.query(models.CorridorSlot).filter_by(slot_id=slot_id).first()
    if not slot:
        raise ValueError(f"slot {slot_id} not found")

    end_time = start_time + dt.timedelta(hours=task.required_duration_hours)
    _hard_safety_check(db, task, slot, start_time, end_time)
    crew_violation = crew_capacity.manual_placement_violation(db, plan_id, task, start_time, end_time)
    if crew_violation:
        raise ValueError(crew_violation)

    warnings, ceiling_hit, overlapping = _soft_conflicts(db, plan, task, start_time, end_time)
    if ceiling_hit:
        occupants = ", ".join(sorted({o.task_id for o in overlapping}))
        raise ValueError(
            f"corridor {task.corridor_id} window is at its absolute capacity ceiling "
            f"({MANUAL_OVERRIDE_HARD_CONCURRENCY_CEILING}) — specify which existing task to displace via "
            f"POST /api/schedule/manual-swap (currently occupying: {occupants})"
        )

    now = utc_now()
    entry = models.BlockPlanEntry(
        plan_id=plan_id, task_id=task_id, slot_id=slot_id, corridor_id=task.corridor_id,
        department=task.department, assigned_window_start=start_time, assigned_window_end=end_time,
        override=True, override_reason=reason, override_by=user_id, override_at=now,
    )
    db.add(entry)
    task.status = "scheduled"
    task.unscheduled_reason = ""

    if plan.status == "published":
        plan.modified_after_publication = True

    metrics = _recompute_plan_co_scheduled_and_metrics(db, plan)
    db.commit()
    db.refresh(entry)

    return {
        "id": entry.id, "task_id": entry.task_id, "plan_id": plan_id, "slot_id": entry.slot_id,
        "corridor_id": entry.corridor_id, "department": entry.department,
        "assigned_window_start": entry.assigned_window_start, "assigned_window_end": entry.assigned_window_end,
        "override": True, "override_reason": entry.override_reason, "override_by": entry.override_by,
        "override_at": entry.override_at, "warnings": warnings,
        "plan_status": plan.status, "modified_after_publication": plan.modified_after_publication,
        "metrics": metrics,
    }


def manual_unschedule_task(db: Session, plan_id: str, task_id: str, user_id: str, reason: str) -> dict:
    """Removes task_id's BlockPlanEntry from the plan and marks the task
    'manually_removed' — deliberately distinct from 'unscheduled' (the
    optimizer tried and had no room) so the task list can tell the two
    apart at a glance."""
    plan = _load_active_plan_for_override(db, plan_id)

    entry = db.query(models.BlockPlanEntry).filter_by(plan_id=plan_id, task_id=task_id).first()
    if not entry:
        raise ValueError(f"task {task_id} has no entry in plan {plan_id}")

    task = db.query(models.MaintenanceTask).filter_by(task_id=task_id).first()
    if not task:
        raise ValueError(f"task {task_id} not found")

    db.delete(entry)
    task.status = "manually_removed"
    task.unscheduled_reason = f"Manually removed by {user_id}: {reason}"

    if plan.status == "published":
        plan.modified_after_publication = True

    metrics = _recompute_plan_co_scheduled_and_metrics(db, plan)
    db.commit()

    return {
        "task_id": task_id, "plan_id": plan_id, "status": task.status, "unscheduled_reason": task.unscheduled_reason,
        "plan_status": plan.status, "modified_after_publication": plan.modified_after_publication,
        "metrics": metrics,
    }


def manual_swap_tasks(db: Session, plan_id: str, incoming_task_id: str, outgoing_task_id: str, slot_id: str, start_time: dt.datetime, user_id: str, reason: str) -> dict:
    """Displaces outgoing_task_id out of the plan and places incoming_task_id
    into slot_id/start_time in its place — a single atomic operation (one
    commit, one audit entry expected from the caller) rather than two
    separate manual-assign/-unschedule calls, so a swap can never be
    observed half-done."""
    plan = _load_active_plan_for_override(db, plan_id)

    if incoming_task_id == outgoing_task_id:
        raise ValueError("incoming and outgoing task cannot be the same task")

    outgoing_entry = db.query(models.BlockPlanEntry).filter_by(plan_id=plan_id, task_id=outgoing_task_id).first()
    if not outgoing_entry:
        raise ValueError(f"outgoing task {outgoing_task_id} has no entry in plan {plan_id} to displace")

    incoming_task = db.query(models.MaintenanceTask).filter_by(task_id=incoming_task_id).first()
    if not incoming_task:
        raise ValueError(f"incoming task {incoming_task_id} not found")

    if db.query(models.BlockPlanEntry).filter_by(plan_id=plan_id, task_id=incoming_task_id).first():
        raise ValueError(f"incoming task {incoming_task_id} already has an entry in this plan")

    slot = db.query(models.CorridorSlot).filter_by(slot_id=slot_id).first()
    if not slot:
        raise ValueError(f"slot {slot_id} not found")

    outgoing_task = db.query(models.MaintenanceTask).filter_by(task_id=outgoing_task_id).first()

    end_time = start_time + dt.timedelta(hours=incoming_task.required_duration_hours)
    _hard_safety_check(db, incoming_task, slot, start_time, end_time)
    # The outgoing entry frees its gang as part of this same swap (only
    # matters when it's the same department — excluding it otherwise is a
    # no-op since the check only counts same-department work).
    crew_violation = crew_capacity.manual_placement_violation(
        db, plan_id, incoming_task, start_time, end_time, exclude_entry_ids=[outgoing_entry.id]
    )
    if crew_violation:
        raise ValueError(crew_violation)

    # The outgoing entry is being removed as part of THIS same swap, so it's
    # excluded from its own capacity/exclusion count — checking against it
    # would make every swap look like it's displacing itself.
    warnings, ceiling_hit, overlapping = _soft_conflicts(
        db, plan, incoming_task, start_time, end_time, exclude_entry_id=outgoing_entry.id
    )
    if ceiling_hit:
        occupants = ", ".join(sorted({o.task_id for o in overlapping}))
        raise ValueError(
            f"corridor {incoming_task.corridor_id} window would still be at its absolute capacity ceiling "
            f"({MANUAL_OVERRIDE_HARD_CONCURRENCY_CEILING}) even after displacing {outgoing_task_id} — "
            f"currently occupying: {occupants}"
        )

    db.delete(outgoing_entry)
    if outgoing_task:
        outgoing_task.status = "displaced"
        outgoing_task.unscheduled_reason = (
            f"Displaced by manual override: {incoming_task_id} assigned by {user_id} into this window (reason: {reason})"
        )

    now = utc_now()
    new_entry = models.BlockPlanEntry(
        plan_id=plan_id, task_id=incoming_task_id, slot_id=slot_id, corridor_id=incoming_task.corridor_id,
        department=incoming_task.department, assigned_window_start=start_time, assigned_window_end=end_time,
        override=True, override_reason=reason, override_by=user_id, override_at=now,
    )
    db.add(new_entry)
    incoming_task.status = "scheduled"
    incoming_task.unscheduled_reason = ""

    if plan.status == "published":
        plan.modified_after_publication = True

    metrics = _recompute_plan_co_scheduled_and_metrics(db, plan)
    db.commit()
    db.refresh(new_entry)

    return {
        "plan_id": plan_id,
        "incoming": {
            "id": new_entry.id, "task_id": new_entry.task_id, "assigned_window_start": new_entry.assigned_window_start,
            "assigned_window_end": new_entry.assigned_window_end, "override": True, "override_reason": reason,
            "override_by": user_id, "override_at": new_entry.override_at,
        },
        "outgoing": {
            "task_id": outgoing_task_id,
            "status": outgoing_task.status if outgoing_task else "displaced",
            "unscheduled_reason": outgoing_task.unscheduled_reason if outgoing_task else "",
        },
        "warnings": warnings, "plan_status": plan.status, "modified_after_publication": plan.modified_after_publication,
        "metrics": metrics,
    }


def _lightweight_reasoning(db: Session, plan: models.BlockPlan, task: models.MaintenanceTask) -> dict:
    """The "what did the optimizer think about this task" half of
    override-impact, WITHOUT decision_intelligence.explain_task's
    counterfactual re-solves — those run scheduler.simulate_schedule (a full
    CP-SAT solve, up to SOLVE_TIME_LIMIT_SECONDS each) TWICE, which is fine
    for the on-demand /api/decision/explain/{task_id} endpoint but would
    make this dry run — called LIVE as the COA picks a task in the
    manual-assign modal — take 20-40+ seconds per keystroke. Everything
    here is a cheap lookup: the task's own already-computed
    unscheduled_reason/priority_reason, or its current plan entry."""
    if task.status == "scheduled":
        entry = db.query(models.BlockPlanEntry).filter_by(plan_id=plan.plan_id, task_id=task.task_id).first()
        if not entry:
            return {"status": task.status, "explanation": "Task is marked scheduled but has no entry in this plan."}
        co_depts = [d for d in entry.co_scheduled_departments.split(",") if d]
        explanation = (
            f"Currently scheduled on corridor {entry.corridor_id} from {entry.assigned_window_start} to "
            f"{entry.assigned_window_end} (priority score {task.priority_score:.1f})."
            + (f" Shares this window with {', '.join(co_depts)}." if co_depts else "")
        )
        return {
            "status": task.status, "priority_score": task.priority_score,
            "assigned_window_start": entry.assigned_window_start, "assigned_window_end": entry.assigned_window_end,
            "co_scheduled_departments": co_depts, "explanation": explanation,
        }

    competing_task = None
    rival_entries = db.query(models.BlockPlanEntry).filter_by(plan_id=plan.plan_id, corridor_id=task.corridor_id).all()
    rival_tasks = [t for t in (db.query(models.MaintenanceTask).filter_by(task_id=e.task_id).first() for e in rival_entries) if t]
    if rival_tasks:
        weakest = min(rival_tasks, key=lambda t: t.priority_score)
        competing_task = {"task_id": weakest.task_id, "department": weakest.department, "priority_score": weakest.priority_score}

    explanation = task.unscheduled_reason or f"Not currently in plan {plan.plan_id} (status: {task.status})."
    if competing_task:
        explanation += (
            f" The lowest-scoring task currently occupying {task.corridor_id} is {competing_task['task_id']} "
            f"({competing_task['department']}, score {competing_task['priority_score']:.1f}) versus this task's "
            f"score of {task.priority_score:.1f}."
        )
    return {
        "status": task.status, "priority_score": task.priority_score,
        "unscheduled_reason": task.unscheduled_reason, "competing_task": competing_task, "explanation": explanation,
    }


def compute_override_impact(db: Session, plan_id: str, task_id: str, slot_id: str, start_time: dt.datetime) -> dict:
    """Read-only dry run for manual_assign_task/manual_swap_tasks: shows
    exactly what WOULD happen without writing anything, so the Control
    Office can see soft-constraint warnings, whether displacement would be
    required, the optimizer's own original reasoning for this task, and the
    proposed window's weather — before confirming a real override."""
    plan = db.query(models.BlockPlan).filter_by(plan_id=plan_id).first()
    if not plan:
        raise ValueError(f"plan {plan_id} not found")

    task = db.query(models.MaintenanceTask).filter_by(task_id=task_id).first()
    if not task:
        raise ValueError(f"task {task_id} not found")

    slot = db.query(models.CorridorSlot).filter_by(slot_id=slot_id).first()
    if not slot:
        raise ValueError(f"slot {slot_id} not found")

    end_time = start_time + dt.timedelta(hours=task.required_duration_hours)

    hard_error = None
    try:
        _hard_safety_check(db, task, slot, start_time, end_time)
    except ValueError as e:
        hard_error = str(e)

    existing_entry = db.query(models.BlockPlanEntry).filter_by(plan_id=plan_id, task_id=task_id).first()
    # Crew capacity is HARD for a manual override too (Feature 10).
    crew_violation = crew_capacity.manual_placement_violation(
        db, plan_id, task, start_time, end_time, exclude_entry_ids=[existing_entry.id] if existing_entry else []
    )
    if hard_error is None and crew_violation:
        hard_error = crew_violation
    warnings, ceiling_hit, overlapping = [], False, []
    if hard_error is None:
        warnings, ceiling_hit, overlapping = _soft_conflicts(
            db, plan, task, start_time, end_time, exclude_entry_id=existing_entry.id if existing_entry else None
        )

    optimizer_reasoning = _lightweight_reasoning(db, plan, task)

    weather_rows = weather_service.hourly_rows_for_window(db, slot.corridor_id, start_time, end_time)
    fc = weather_service._window_forecast(weather_rows, start_time, end_time)
    weather_summary = None
    if fc is not None:
        weather_summary = {
            "precipitation_probability_pct": fc.precipitation_probability_pct,
            "wind_speed_kmh": fc.wind_speed_kmh,
            "visibility_km": fc.visibility_km,
            "lightning_risk": fc.lightning_risk,
            "fog_risk": fc.fog_risk,
            "temperature_max_c": fc.temperature_max_c,
            "granularity": "hourly" if weather_rows else "daily",
        }

    return {
        "plan_id": plan_id, "task_id": task_id, "slot_id": slot_id,
        "proposed_start": start_time, "proposed_end": end_time,
        "would_succeed": hard_error is None and not ceiling_hit,
        "hard_safety_violation": hard_error,
        "crew_capacity_violation": crew_violation,
        "soft_warnings": warnings,
        "displacement_required": ceiling_hit,
        "displaceable_candidates": sorted({o.task_id for o in overlapping}) if ceiling_hit else [],
        "optimizer_reasoning": optimizer_reasoning,
        "weather": weather_summary,
    }
