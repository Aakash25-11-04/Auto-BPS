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

import metrics as metrics_mod
import models
import timetable_loader

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
COORDINATION_BONUS = 40.0  # objective points per genuinely-overlapping cross-department task pair
SCALE = 100  # CP-SAT needs integer coefficients; scores carry 1 decimal of meaning
SOLVE_TIME_LIMIT_SECONDS = 20.0  # leaves headroom under the 30s requirement for
# model-build + plan-materialization overhead, which CP-SAT's own time limit
# does not cover — measured up to ~5s on a 31-task/60-window monthly instance

HORIZON_DAYS = {"weekly": 7, "monthly": 30}


def _duration_hours(slot) -> float:
    return (slot.end_time - slot.start_time).total_seconds() / 3600.0


def _slot_is_safe(db: Session, slot, horizon_start: dt.date, horizon_days: int) -> bool:
    """Re-validates a candidate slot against the real timetable directly
    (not just trusting that it was derived from a gap) — this is what
    catches a manually-entered COA window that happens to clash with a
    real train movement. Because a task's interval is always constrained to
    sit fully inside whichever window it's placed in, filtering out unsafe
    windows here is sufficient to guarantee no interval ever overlaps a real
    train movement — no further per-interval train check is needed."""
    occurrences = timetable_loader.corridor_occurrences(db, slot.corridor_id, horizon_start, horizon_days)
    for occ in occurrences:
        if occ["start"] < slot.end_time and slot.start_time < occ["end"]:
            return False
    return True


def get_safe_slots(db: Session, horizon: str, horizon_start: dt.date = None):
    """Returns (safe_slots, unsafe_excluded_count) for a horizon — shared by
    the real optimizer and by baseline.py's manual-process simulation so
    both draw from exactly the same, timetable-validated pool of windows."""
    if horizon not in HORIZON_DAYS:
        raise ValueError("horizon must be 'weekly' or 'monthly'")
    horizon_days = HORIZON_DAYS[horizon]
    horizon_start = horizon_start or dt.date.today()
    all_slots = (
        db.query(models.CorridorSlot)
        .filter(models.CorridorSlot.horizon == horizon, models.CorridorSlot.status != "cancelled")
        .all()
    )
    safe_slots = [s for s in all_slots if _slot_is_safe(db, s, horizon_start, horizon_days)]
    return safe_slots, len(all_slots) - len(safe_slots)


def _build_reason(task, eligible_slots_exist: bool) -> str:
    if not eligible_slots_exist:
        return (
            f"No corridor slot on {task.corridor_id} long enough for the required "
            f"{task.required_duration_hours:g}h within this horizon (corridor unavailability)."
        )
    return "Lower relative priority than competing tasks contending for the same available slots."


def _minutes(when: dt.datetime, epoch: dt.datetime) -> int:
    return int(round((when - epoch).total_seconds() / 60.0))


def _from_minutes(minutes: int, epoch: dt.datetime) -> dt.datetime:
    return epoch + dt.timedelta(minutes=minutes)


def run_schedule(db: Session, horizon: str, horizon_start: dt.date = None, corridor_capacity: int = DEFAULT_CORRIDOR_CAPACITY) -> dict:
    if horizon not in HORIZON_DAYS:
        raise ValueError("horizon must be 'weekly' or 'monthly'")
    horizon_days = HORIZON_DAYS[horizon]
    horizon_start = horizon_start or dt.date.today()

    tasks = (
        db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.status.in_(["submitted", "unscheduled", "scheduled"]))
        .all()
    )
    safe_slots, unsafe_excluded = get_safe_slots(db, horizon, horizon_start)

    # eligibility: (task, window) pairs where corridor matches and window is long enough
    eligible = {}
    for t in tasks:
        matches = [s for s in safe_slots if s.corridor_id == t.corridor_id and _duration_hours(s) >= t.required_duration_hours]
        eligible[t.task_id] = matches

    epoch = dt.datetime.combine(horizon_start, dt.time.min)

    start = time.time()
    try:
        assignment, engine_used, solver_status = _solve_cp_sat(tasks, safe_slots, eligible, epoch, corridor_capacity)
    except Exception:
        assignment, engine_used, solver_status = _solve_greedy(tasks, safe_slots, eligible, epoch, corridor_capacity)
    solve_time = time.time() - start

    return _materialize_plan(
        db, horizon, tasks, safe_slots, eligible, assignment, engine_used, solver_status, solve_time, unsafe_excluded
    )


def _solve_cp_sat(tasks, slots, eligible, epoch, corridor_capacity):
    if _cp_model is None:
        raise RuntimeError("OR-Tools is not available in this environment")
    cp_model = _cp_model

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
        duration_min = int(round(t.required_duration_hours * 60))
        for s in eligible[t.task_id]:
            w_start = _minutes(s.start_time, epoch)
            w_end = _minutes(s.end_time, epoch)
            latest_start = w_end - duration_min
            if latest_start < w_start:
                continue
            start_var = model.NewIntVar(w_start, latest_start, f"start_{t.task_id}_{s.slot_id}")
            end_var = model.NewIntVar(w_start + duration_min, w_end, f"end_{t.task_id}_{s.slot_id}")
            presence = model.NewBoolVar(f"present_{t.task_id}_{s.slot_id}")
            interval = model.NewOptionalIntervalVar(start_var, duration_min, end_var, presence, f"iv_{t.task_id}_{s.slot_id}")
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

    objective_terms = []
    for t in tasks:
        score_int = int(round(t.priority_score * SCALE))
        objective_terms.append(score_int * scheduled[t.task_id])
    for overlap in overlap_terms:
        objective_terms.append(int(COORDINATION_BONUS * SCALE) * overlap)

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


def _solve_greedy(tasks, slots, eligible, epoch, corridor_capacity):
    """Deterministic fallback: highest priority first, actual interval
    placement (not whole-window assignment). For each task, tries its
    eligible windows (preferring ones that already host a different
    department, to actively chase coordination) and, within a window,
    scans candidate start times — window start plus every existing
    interval's end time that could still fit — picking the earliest one
    that both respects the corridor's concurrency capacity and doesn't
    overlap any mutually-exclusive partner already placed in any window.
    Never hard-fails; a task simply stays unscheduled if no window has room."""
    tasks_by_id = {t.task_id: t for t in tasks}
    placed = []  # list of dicts: task_id, slot_id, corridor_id, start_min, end_min, department
    assignment = {}

    def excluded_ids(t):
        return set(x.strip() for x in (t.mutually_exclusive_with or "").split(",") if x.strip())

    ordered = sorted(tasks, key=lambda t: t.priority_score, reverse=True)

    for t in ordered:
        duration_min = int(round(t.required_duration_hours * 60))
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
            w_start_min = _minutes(w.start_time, epoch)
            w_end_min = _minutes(w.end_time, epoch)
            latest_start = w_end_min - duration_min
            if latest_start < w_start_min:
                continue

            same_corridor_same_window = [p for p in placed if p["slot_id"] == w.slot_id]
            relevant_excl = [p for p in excl_partners_global]

            breakpoints = {w_start_min}
            for p in same_corridor_same_window + relevant_excl:
                if w_start_min <= p["end_min"] <= latest_start:
                    breakpoints.add(p["end_min"])
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


def _materialize_plan(db, horizon, tasks, slots, eligible, assignment, engine_used, solver_status, solve_time, unsafe_excluded):
    slot_by_id = {s.slot_id: s for s in slots}
    tasks_by_id = {t.task_id: t for t in tasks}

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
    scheduled_list = []
    for task_id, (slot_id, start_dt, end_dt) in assignment.items():
        task = tasks_by_id[task_id]
        scheduled_list.append(
            {
                "task_id": task_id,
                "department": task.department,
                "corridor_id": slot_by_id[slot_id].corridor_id,
                "slot_id": slot_id,
                "start": start_dt,
                "end": end_dt,
                "duration_hours": task.required_duration_hours,
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
        entry = models.BlockPlanEntry(
            plan_id=plan_id,
            task_id=e["task_id"],
            slot_id=e["slot_id"],
            corridor_id=e["corridor_id"],
            department=e["department"],
            assigned_window_start=e["start"],
            assigned_window_end=e["end"],
            co_scheduled_departments=",".join(sorted(co_scheduled[e["task_id"]])),
        )
        db.add(entry)
        task.status = "scheduled"
        task.unscheduled_reason = ""
        scheduled_ids.add(e["task_id"])
        total_score_scheduled += task.priority_score

    unscheduled = []
    for t in tasks:
        if t.task_id in scheduled_ids:
            continue
        eligible_exists = len(eligible.get(t.task_id, [])) > 0
        reason = _build_reason(t, eligible_exists)
        t.status = "unscheduled"
        t.unscheduled_reason = reason
        unscheduled.append({"task_id": t.task_id, "department": t.department, "corridor_id": t.corridor_id, "reason": reason})

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
    }
    plan.metrics_json = json.dumps(metrics)

    db.commit()

    return {"plan_id": plan_id, "version": plan.version, "metrics": metrics, "unscheduled": unscheduled}


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
    # run happened to use (which isn't persisted with the plan).
    occurrences = timetable_loader.corridor_occurrences(db, entry.corridor_id, new_start.date(), 2)
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
