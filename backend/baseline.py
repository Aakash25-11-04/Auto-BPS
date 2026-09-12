"""Simulates today's MANUAL block-scheduling process — the "before" picture
against which the optimizer's "after" is measured (FR-COA-06).

This is a pure, read-only, in-memory simulation. It never writes a BlockPlan
or BlockPlanEntry and never changes a MaintenanceTask's status — running it
has zero side effects on the real system, so it's safe to recompute on every
GET /api/schedule/comparison call.

Modelling assumptions (deliberately conservative, not a strawman built to
lose — see README.md for the same explanation in user-facing docs):

  1. Departments act independently and sequentially: ENG is scheduled first,
     then TD, then SNT. In real wall-clock time departments don't literally
     take turns, but "no cross-department awareness" cashes out to exactly
     this: whichever request lands on a window first claims it outright, and
     later departments have no visibility into what's already been granted
     until they go looking for their own next window.

  2. Within a department, tasks are processed in SUBMISSION order
     (created_at ascending) — not priority order. A manual process is
     first-come-first-served; it has no systematic urgency ranking. Ordering
     the baseline by priority would hand it foreknowledge the real manual
     process doesn't have and unfairly flatter it.

  3. Each task claims the EARLIEST remaining window on its corridor that is
     long enough for its own duration, and reserves that window'S ENTIRE
     span exclusively for its own department — once claimed, no other task,
     from any department, may use any leftover time in it. This is the
     specific, real inefficiency ABPS eliminates: a maintenance block is
     granted as one indivisible closure per request in the manual process,
     so a 2-hour task in a 6-hour window wastes the other 4 hours. That
     waste is the whole reason total_downtime_hours is computed as the
     window's FULL duration here (see metrics.py's full_window_downtime
     flag), not just the task's own required time.

  4. The same real-timetable safety filtering the optimizer uses (windows
     that would overlap a real train movement are never offered) applies
     here too, so the comparison is apples-to-apples rather than baseline
     being handicapped by an unfair pool of windows.
"""
import datetime as dt

from sqlalchemy.orm import Session

import metrics as metrics_mod
import models
from scheduler import DEPARTMENTS, _duration_hours, get_safe_slots

HORIZON_DAYS = {"weekly": 7, "monthly": 30}


def run_baseline(db: Session, horizon: str, horizon_start: dt.date = None) -> dict:
    if horizon not in HORIZON_DAYS:
        raise ValueError("horizon must be 'weekly' or 'monthly'")

    tasks = (
        db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.status.in_(["submitted", "unscheduled", "scheduled"]))
        .all()
    )
    safe_slots, _unsafe_excluded = get_safe_slots(db, horizon, horizon_start)

    claimed_slot_ids = set()
    slots_by_corridor = {}
    for s in safe_slots:
        slots_by_corridor.setdefault(s.corridor_id, []).append(s)
    for corridor_id in slots_by_corridor:
        slots_by_corridor[corridor_id].sort(key=lambda s: s.start_time)

    tasks_by_department = {d: [] for d in DEPARTMENTS}
    for t in tasks:
        if t.department in tasks_by_department:
            tasks_by_department[t.department].append(t)
    for d in tasks_by_department:
        tasks_by_department[d].sort(key=lambda t: t.created_at)

    scheduled_list = []
    unscheduled = []

    for department in DEPARTMENTS:  # ENG, then TD, then SNT — sequential, no shared awareness
        for t in tasks_by_department[department]:
            candidates = [
                s
                for s in slots_by_corridor.get(t.corridor_id, [])
                if s.slot_id not in claimed_slot_ids and _duration_hours(s) >= t.required_duration_hours
            ]
            if not candidates:
                unscheduled.append({"task_id": t.task_id, "department": t.department, "corridor_id": t.corridor_id})
                continue

            window = candidates[0]  # earliest available window that fits
            claimed_slot_ids.add(window.slot_id)
            scheduled_list.append(
                {
                    "task_id": t.task_id,
                    "department": t.department,
                    "corridor_id": window.corridor_id,
                    "slot_id": window.slot_id,
                    "start": window.start_time,  # baseline blocks the WHOLE window for the task
                    "end": window.end_time,
                    "duration_hours": t.required_duration_hours,  # the task's real work time, not the reserved span
                    "priority_score": t.priority_score,
                    "overdue_days": t.overdue_days,
                }
            )

    computed = metrics_mod.compute_metrics(scheduled_list, tasks, safe_slots, full_window_downtime=True)

    return {
        "horizon": horizon,
        "metrics": computed,
        "scheduled": scheduled_list,
        "unscheduled": unscheduled,
    }
