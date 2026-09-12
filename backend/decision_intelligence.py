"""Layer 5 — Decision Intelligence. The differentiating layer: turns ABPS
from a scheduler into an advisor. Four capabilities, all built on
scheduler.simulate_schedule() (which never writes to the database) except
emergency re-optimization, which deliberately DOES write a new plan version
— it is a real re-solve, not a preview.
"""
import datetime as dt
import uuid

import models
import priority_engine
import scheduler
from audit import log

DEPARTMENTS = ["ENG", "TD", "SNT"]


# ============================================================ Emergency re-optimization

def emergency_reoptimize(db, horizon: str, emergency_payload: dict, user_id: str) -> dict:
    """Injects an urgent defect and re-solves with a deviation penalty
    against the currently-published plan, so gangs who already planned
    around the published schedule aren't disrupted unless the emergency's
    priority genuinely justifies it. Returns a diff report: which tasks
    stayed, which moved, and which were newly bumped."""
    published = (
        db.query(models.BlockPlan).filter_by(horizon=horizon, status="published").order_by(models.BlockPlan.version.desc()).first()
    )
    previous_assignment = {}
    previous_entries_by_task = {}
    if published:
        for e in db.query(models.BlockPlanEntry).filter_by(plan_id=published.plan_id).all():
            previous_assignment[e.task_id] = (e.assigned_window_start, e.assigned_window_end)
            previous_entries_by_task[e.task_id] = e

    task_id = f"task-{emergency_payload['department']}-{uuid.uuid4().hex[:8]}"
    task = models.MaintenanceTask(
        task_id=task_id,
        department=emergency_payload["department"],
        asset_id=emergency_payload["asset_id"],
        defect_type=emergency_payload["defect_type"],
        severity=emergency_payload.get("severity", 5),
        overdue_days=emergency_payload.get("overdue_days", 0),
        required_duration_hours=emergency_payload["required_duration_hours"],
        corridor_id=emergency_payload["corridor_id"],
        safety_critical=emergency_payload.get("safety_critical", True),
        status="submitted",
        source="manual",
        source_ref="emergency_injection",
        created_by=user_id,
    )
    priority_engine.rescore_task(db, task)
    db.add(task)
    db.commit()

    stability_bonus = emergency_payload.get("stability_bonus", 60.0)
    result = scheduler.run_schedule(
        db, horizon, previous_assignment=previous_assignment, stability_bonus=stability_bonus
    )

    new_plan_entries = {
        e.task_id: e
        for e in db.query(models.BlockPlanEntry).filter_by(plan_id=result["plan_id"]).all()
    }

    moved, unchanged, newly_unscheduled, newly_scheduled = [], [], [], []
    for tid, prev_entry in previous_entries_by_task.items():
        new_entry = new_plan_entries.get(tid)
        if new_entry is None:
            newly_unscheduled.append(tid)
        elif new_entry.assigned_window_start == prev_entry.assigned_window_start:
            unchanged.append(tid)
        else:
            moved.append({"task_id": tid, "old_start": prev_entry.assigned_window_start, "new_start": new_entry.assigned_window_start})
    for tid in new_plan_entries:
        if tid not in previous_entries_by_task:
            newly_scheduled.append(tid)

    log(
        db, "emergency_reoptimization", user_id,
        {"emergency_task_id": task_id, "horizon": horizon, "plan_id": result["plan_id"], "moved_count": len(moved), "unchanged_count": len(unchanged)},
    )

    return {
        "emergency_task_id": task_id,
        "plan_id": result["plan_id"],
        "metrics": result["metrics"],
        "emergency_task_scheduled": task_id in new_plan_entries,
        "stability_bonus_used": stability_bonus,
        "tasks_unchanged": len(unchanged),
        "tasks_moved": moved,
        "tasks_newly_unscheduled": newly_unscheduled,
        "tasks_newly_scheduled_besides_emergency": [t for t in newly_scheduled if t != task_id],
        "previously_published_plan_id": published.plan_id if published else None,
    }


# ============================================================ What-if analysis

def run_what_if(db, horizon: str, scenarios: list) -> dict:
    """Runs N named scenario configs through simulate_schedule (read-only —
    nothing here is ever persisted) and returns them side by side so the
    Control Office can compare BEFORE approving anything. Each scenario dict
    may set: corridor_capacity, exclude_corridors (list), coordination_bonus,
    extra_slot_hours ({corridor_id: hours})."""
    results = []
    for scenario in scenarios:
        name = scenario.get("name", "scenario")
        sim = scheduler.simulate_schedule(
            db, horizon,
            corridor_capacity=scenario.get("corridor_capacity", scheduler.DEFAULT_CORRIDOR_CAPACITY),
            exclude_corridors=scenario.get("exclude_corridors"),
            coordination_bonus=scenario.get("coordination_bonus"),
            extra_slot_hours=scenario.get("extra_slot_hours"),
        )
        results.append({"name": name, "config": scenario, **sim})
    return {"horizon": horizon, "scenarios": results}


# ============================================================ Explainability / counterfactual

def explain_task(db, horizon: str, task_id: str) -> dict:
    task = db.query(models.MaintenanceTask).filter_by(task_id=task_id).first()
    if not task:
        raise ValueError(f"task '{task_id}' not found")

    if task.status == "scheduled":
        plan = (
            db.query(models.BlockPlan)
            .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
            .order_by(models.BlockPlan.version.desc())
            .first()
        )
        entry = db.query(models.BlockPlanEntry).filter_by(plan_id=plan.plan_id, task_id=task_id).first() if plan else None
        if not entry:
            return {"task_id": task_id, "status": task.status, "explanation": "Task is marked scheduled but has no entry in the current plan — run the scheduler again."}
        co_depts = [d for d in entry.co_scheduled_departments.split(",") if d]
        explanation = (
            f"Scheduled on corridor {entry.corridor_id} from {entry.assigned_window_start} to {entry.assigned_window_end} "
            f"because its priority score ({task.priority_score:.1f}) secured it a place within the corridor's capacity "
            f"({scheduler.DEFAULT_CORRIDOR_CAPACITY} concurrent work parties) inside a window that is safe against the "
            f"real train timetable."
            + (f" It shares this window with {', '.join(co_depts)}, earning the plan a coordination bonus." if co_depts else " No other department shares this exact window.")
        )
        return {
            "task_id": task_id, "status": "scheduled", "priority_score": task.priority_score,
            "priority_reason": task.priority_reason, "assigned_window_start": entry.assigned_window_start,
            "assigned_window_end": entry.assigned_window_end, "co_scheduled_departments": co_depts,
            "explanation": explanation,
        }

    # ---- unscheduled: counterfactual + named competitor ----
    safe_slots, _ = scheduler.get_safe_slots(db, horizon)
    eligible_windows = [
        s for s in safe_slots
        if s.corridor_id == task.corridor_id and scheduler._duration_hours(s) >= task.required_duration_hours
    ]

    competing_task = None
    if eligible_windows:
        plan = (
            db.query(models.BlockPlan)
            .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
            .order_by(models.BlockPlan.version.desc())
            .first()
        )
        if plan:
            rival_entries = (
                db.query(models.BlockPlanEntry)
                .filter_by(plan_id=plan.plan_id, corridor_id=task.corridor_id)
                .all()
            )
            rival_tasks = [
                db.query(models.MaintenanceTask).filter_by(task_id=e.task_id).first() for e in rival_entries
            ]
            rival_tasks = [t for t in rival_tasks if t]
            if rival_tasks:
                weakest = min(rival_tasks, key=lambda t: t.priority_score)
                competing_task = {"task_id": weakest.task_id, "department": weakest.department, "priority_score": weakest.priority_score}

    # Deliberately NOT gated on eligible_windows: that check uses the task's
    # CURRENT duration, but the whole point of the duration counterfactual is
    # asking whether a DIFFERENT duration would have found a window — exactly
    # the "corridor unavailability" case this would otherwise skip testing.
    counterfactuals = [_try_counterfactual(db, horizon, task, severity=min(5, task.severity + 2))]
    if task.required_duration_hours > 1:
        counterfactuals.append(_try_counterfactual(db, horizon, task, required_duration_hours=round(task.required_duration_hours / 2, 1)))

    explanation = task.unscheduled_reason
    if competing_task:
        explanation += (
            f" The lowest-scoring task currently occupying that corridor is {competing_task['task_id']} "
            f"({competing_task['department']}, score {competing_task['priority_score']:.1f}) versus this task's "
            f"score of {task.priority_score:.1f}."
        )

    return {
        "task_id": task_id, "status": task.status, "priority_score": task.priority_score,
        "unscheduled_reason": task.unscheduled_reason, "competing_task": competing_task,
        "counterfactuals": counterfactuals, "explanation": explanation,
    }


def _try_counterfactual(db, horizon: str, task: models.MaintenanceTask, **overrides) -> dict:
    """Actually re-solves with the task's severity/duration temporarily
    changed (never committed — always rolled back) to test whether that
    specific change would genuinely have gotten it scheduled. Not a guess:
    a real CP-SAT re-solve decides the answer."""
    original = {k: getattr(task, k) for k in overrides}
    original_score, original_reason = task.priority_score, task.priority_reason
    try:
        for k, v in overrides.items():
            setattr(task, k, v)
        priority_engine.rescore_task(db, task)
        sim = scheduler.simulate_schedule(db, horizon)
        would_be_scheduled = task.task_id in sim["scheduled_task_ids"]
        return {
            "change": overrides,
            "new_priority_score": round(task.priority_score, 1),
            "would_be_scheduled": would_be_scheduled,
            "statement": (
                f"would have been scheduled if {', '.join(f'{k} were {v}' for k, v in overrides.items())} "
                f"(new score {task.priority_score:.1f})"
                if would_be_scheduled
                else f"still would NOT be scheduled even if {', '.join(f'{k} were {v}' for k, v in overrides.items())}"
            ),
        }
    finally:
        for k, v in original.items():
            setattr(task, k, v)
        task.priority_score, task.priority_reason = original_score, original_reason
        db.rollback()


# ============================================================ Shadow prices

def compute_shadow_price(db, horizon: str, corridor_id: str, extra_hours: float = 2.0) -> dict:
    """The marginal value of extra_hours more corridor time on corridor_id:
    re-solves once as-is and once with every safe slot on that corridor
    extended by extra_hours, and reports the difference in tasks scheduled
    and priority points captured. Read-only (simulate_schedule never
    writes)."""
    baseline = scheduler.simulate_schedule(db, horizon)
    with_extra = scheduler.simulate_schedule(db, horizon, extra_slot_hours={corridor_id: extra_hours})

    newly_scheduled = sorted(set(with_extra["scheduled_task_ids"]) - set(baseline["scheduled_task_ids"]))
    score_gain = round(with_extra["total_objective_priority_score"] - baseline["total_objective_priority_score"], 1)

    return {
        "corridor_id": corridor_id,
        "extra_hours": extra_hours,
        "baseline_tasks_scheduled": len(baseline["scheduled_task_ids"]),
        "with_extra_tasks_scheduled": len(with_extra["scheduled_task_ids"]),
        "additional_tasks_scheduled": len(newly_scheduled),
        "newly_scheduled_task_ids": newly_scheduled,
        "priority_points_gained": score_gain,
        "statement": (
            f"{extra_hours:g} more hour(s) on {corridor_id} would allow {len(newly_scheduled)} more task(s) "
            f"worth {score_gain:.1f} priority points."
            if newly_scheduled
            else f"{extra_hours:g} more hour(s) on {corridor_id} would not change how many tasks get scheduled "
            "— capacity/mutual-exclusion, not corridor time, is the binding constraint right now."
        ),
    }
