"""Shared before/after metrics computation.

Used by BOTH the real optimizer (scheduler.py's _materialize_plan) and the
manual-process baseline simulation (baseline.py), so the two are always
measured with identical definitions and a comparison between them is
apples-to-apples rather than two different yardsticks.

Every metric here is computed from a plain list of "scheduled entry" dicts,
not from ORM objects directly, so the same function works whether the
entries came from a freshly-solved CP-SAT/greedy plan or from the baseline's
in-memory simulation (which never touches the database).
"""
import datetime as dt


def compute_metrics(scheduled: list, all_tasks: list, safe_slots: list, full_window_downtime: bool = False) -> dict:
    """
    scheduled: list of dicts, each with keys
        task_id, department, corridor_id, slot_id, start (datetime),
        end (datetime), priority_score (float), overdue_days (int)
    all_tasks: every MaintenanceTask considered for this horizon (for tasks_total)
    safe_slots: every timetable-safe CorridorSlot available this horizon
        (the denominator for utilization — the total resource pool, which
        doesn't depend on how it was used)
    full_window_downtime: if True, a window's downtime contribution is its
        FULL nominal duration regardless of how much of it real work
        occupied — this is the manual-process baseline's defining
        inefficiency (a granted closure is reserved for that department's
        exclusive use, full stop). If False (the optimizer's case), downtime
        is the genuine span from the earliest task's start to the latest
        task's end within that window — the honest cost of the closure ABPS
        actually needed to request.
    """
    slot_by_id = {s.slot_id: s for s in safe_slots}
    total_available_hours = sum((s.end_time - s.start_time).total_seconds() / 3600.0 for s in safe_slots)

    by_window = {}
    for entry in scheduled:
        by_window.setdefault(entry["slot_id"], []).append(entry)

    total_downtime_hours = 0.0
    coordinated_blocks = 0
    for slot_id, entries in by_window.items():
        if full_window_downtime and slot_id in slot_by_id:
            slot = slot_by_id[slot_id]
            span_hours = (slot.end_time - slot.start_time).total_seconds() / 3600.0
        else:
            starts = [e["start"] for e in entries]
            ends = [e["end"] for e in entries]
            span_hours = (max(ends) - min(starts)).total_seconds() / 3600.0
        total_downtime_hours += span_hours

        # A window counts as "coordinated" only if two DIFFERENT-department
        # tasks in it genuinely overlap in time — not merely because they
        # both happen to land in the same nominal window. Those are not the
        # same thing under true interval scheduling: capacity=1 (or any
        # other constraint) can force two different-department tasks into
        # the same window back-to-back with zero actual overlap, and that
        # is not coordination by this system's own definition (see
        # scheduler.py's co_scheduled_departments, computed the same way).
        genuinely_coordinated = False
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a, b = entries[i], entries[j]
                if a["department"] == b["department"]:
                    continue
                if a["start"] < b["end"] and b["start"] < a["end"]:
                    genuinely_coordinated = True
                    break
            if genuinely_coordinated:
                break
        if genuinely_coordinated:
            coordinated_blocks += 1

    # Deliberately NOT (end - start): for a baseline entry that span is the
    # whole reserved window, not genuine work time. duration_hours is always
    # the task's own real required duration, so utilization measures actual
    # productive work against the resource pool regardless of how much of
    # the window a given engine happened to consume around it.
    productive_hours = sum(e["duration_hours"] for e in scheduled)
    tasks_completed = len(scheduled)
    priority_weighted_completion = round(sum(e["priority_score"] for e in scheduled), 1)
    avg_overdue = round(sum(e["overdue_days"] for e in scheduled) / tasks_completed, 1) if tasks_completed else 0.0
    utilization_pct = round((productive_hours / total_available_hours * 100), 1) if total_available_hours else 0.0

    return {
        "tasks_total": len(all_tasks),
        "tasks_completed": tasks_completed,
        "total_corridor_closures": len(by_window),
        "total_downtime_hours": round(total_downtime_hours, 2),
        "priority_weighted_completion": priority_weighted_completion,
        "block_utilization_pct": utilization_pct,
        "coordinated_blocks": coordinated_blocks,
        "avg_overdue_days_of_scheduled_tasks": avg_overdue,
    }
