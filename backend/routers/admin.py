"""Administrator endpoints: users, scoring weights, asset criticality, audit log.

RBAC: everything under /api/admin/* requires ADMIN — except GET
/api/admin/audit-log, which is explicitly ADMIN-and-COA (the Control Office
needs visibility into the operational audit trail — schedule runs,
approvals, rejections — without holding full ADMIN system-configuration
rights). That one route is the single, deliberate, explicitly-stated
exception to the blanket ADMIN-only rule; every other route here uses
require_role("ADMIN") alone.
"""
import datetime as dt
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import crew_capacity
import models
import priority_engine
import schemas
import weather_service
from audit import log
from database import get_db

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users")
def list_users(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_role("ADMIN"))):
    rows = db.query(models.User).all()
    # password_hash is intentionally never included in this response.
    # created_at is naive-but-UTC (see tz_utils.py) and gets the explicit
    # UTC offset added by the global JSON encoder (main.py) — the frontend
    # converts to IST for display like every other timestamp in this app.
    return [
        {"user_id": u.user_id, "name": u.name, "role": u.role, "department": u.department, "active": u.active, "created_at": u.created_at}
        for u in rows
    ]


@router.post("/users")
def create_user(
    payload: schemas.UserCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN")),
):
    if db.query(models.User).filter_by(user_id=payload.user_id).first():
        raise HTTPException(status_code=409, detail="user_id already exists")
    user = models.User(
        user_id=payload.user_id,
        name=payload.name,
        role=payload.role.upper(),
        department=(payload.department or "").upper(),
        active=payload.active,
        password_hash=auth.hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    log(db, "user_created", current_user.user_id, {"user_id": user.user_id, "role": user.role})
    return {"user_id": user.user_id, "name": user.name, "role": user.role, "department": user.department, "active": user.active}


@router.get("/scoring-config")
def get_scoring_config(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_role("ADMIN"))):
    return priority_engine.get_weights(db)


@router.post("/scoring-config")
def set_scoring_config(
    payload: schemas.ScoringConfigUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN")),
):
    if payload.key not in priority_engine.DEFAULT_WEIGHTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown weight key '{payload.key}', expected one of {sorted(priority_engine.DEFAULT_WEIGHTS)}",
        )
    row = db.query(models.ScoringConfig).filter_by(key=payload.key).first()
    if row:
        row.value = payload.value
    else:
        db.add(models.ScoringConfig(key=payload.key, value=payload.value))
    db.commit()

    rescored = priority_engine.rescore_all(db)
    log(db, "scoring_config_changed", current_user.user_id, {"key": payload.key, "value": payload.value, "tasks_rescored": rescored})

    return {"key": payload.key, "value": payload.value, "tasks_rescored": rescored, "weights": priority_engine.get_weights(db)}


@router.get("/asset-criticality")
def list_asset_criticality(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_role("ADMIN"))):
    rows = db.query(models.AssetCriticality).all()
    return [{"asset_id": r.asset_id, "criticality": r.criticality} for r in rows]


@router.post("/asset-criticality")
def set_asset_criticality(
    payload: schemas.AssetCriticalityUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN")),
):
    row = db.query(models.AssetCriticality).filter_by(asset_id=payload.asset_id).first()
    if row:
        row.criticality = payload.criticality
        if payload.asset_type is not None:
            row.asset_type = payload.asset_type
        if payload.age_years is not None:
            row.age_years = payload.age_years
        if payload.historical_failure_count is not None:
            row.historical_failure_count = payload.historical_failure_count
    else:
        db.add(
            models.AssetCriticality(
                asset_id=payload.asset_id,
                criticality=payload.criticality,
                asset_type=payload.asset_type or "",
                age_years=payload.age_years or 0.0,
                historical_failure_count=payload.historical_failure_count or 0,
            )
        )
    db.commit()

    affected = db.query(models.MaintenanceTask).filter_by(asset_id=payload.asset_id).all()
    for t in affected:
        priority_engine.rescore_task(db, t)
    db.commit()

    log(db, "asset_criticality_set", current_user.user_id, {"asset_id": payload.asset_id, "criticality": payload.criticality, "tasks_rescored": len(affected)})
    return {"asset_id": payload.asset_id, "criticality": payload.criticality, "tasks_rescored": len(affected)}


@router.get("/scoring-source")
def get_scoring_source(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_role("ADMIN", "COA"))):
    # Read-only visibility extended to COA (same "elevated visibility, not
    # elevated write access" pattern as the audit log): the Decision
    # Intelligence tools live in the Control Office view and need to show
    # which scoring source is active, even though only ADMIN can change it.
    return {"source": priority_engine.get_scoring_source(db), "valid_sources": priority_engine.VALID_SCORING_SOURCES, "can_change": current_user.role == "ADMIN"}


@router.post("/scoring-source")
def set_scoring_source(
    payload: schemas.ScoringSourceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN")),
):
    try:
        priority_engine.set_scoring_source(db, payload.source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    rescored = priority_engine.rescore_all(db)
    log(db, "scoring_source_changed", current_user.user_id, {"source": payload.source, "tasks_rescored": rescored})
    return {"source": payload.source, "tasks_rescored": rescored}


@router.get("/weather-rules")
def list_weather_rules(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_role("ADMIN", "COA"))):
    # Same "elevated visibility, not elevated write access" pattern as the
    # audit log / scoring-source: COA needs to SEE why a candidate was
    # excluded or buffered without holding ADMIN's config-write rights.
    weather_service.ensure_default_rules(db)
    rows = db.query(models.WeatherSensitivityRule).order_by(models.WeatherSensitivityRule.rule_id).all()
    return [
        {
            "rule_id": r.rule_id, "label": r.label, "defect_type_pattern": r.defect_type_pattern,
            "hazard": r.hazard, "mode": r.mode, "threshold": r.threshold,
            "duration_buffer_pct": r.duration_buffer_pct, "priority_points": r.priority_points,
            "active": r.active,
        }
        for r in rows
    ]


@router.post("/weather-rules")
def upsert_weather_rule(
    payload: schemas.WeatherRuleUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN")),
):
    if payload.hazard not in weather_service.VALID_HAZARDS:
        raise HTTPException(status_code=400, detail=f"hazard must be one of {weather_service.VALID_HAZARDS}")
    if payload.mode not in weather_service.VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {weather_service.VALID_MODES}")

    weather_service.ensure_default_rules(db)
    row = db.query(models.WeatherSensitivityRule).filter_by(rule_id=payload.rule_id).first()
    if row:
        row.label = payload.label or row.label
        row.defect_type_pattern = payload.defect_type_pattern
        row.hazard = payload.hazard
        row.mode = payload.mode
        row.threshold = payload.threshold
        row.duration_buffer_pct = payload.duration_buffer_pct
        row.priority_points = payload.priority_points
        row.active = payload.active
    else:
        db.add(models.WeatherSensitivityRule(**payload.model_dump()))
    db.commit()

    # A hard/soft rule change only affects the NEXT scheduler run (candidate
    # generation happens at solve time, not at rescore time); a priority-mode
    # rule change affects priority scoring immediately, so rescore now — same
    # "changing a weight immediately rescores every task" behavior as the
    # existing scoring-config endpoint.
    rescored = priority_engine.rescore_all(db)
    log(db, "weather_rule_changed", current_user.user_id, {"rule_id": payload.rule_id, "mode": payload.mode, "tasks_rescored": rescored})
    return {"rule_id": payload.rule_id, "tasks_rescored": rescored}


@router.delete("/weather-rules/{rule_id}")
def deactivate_weather_rule(
    rule_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN")),
):
    row = db.query(models.WeatherSensitivityRule).filter_by(rule_id=rule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="rule not found")
    row.active = False
    db.commit()
    rescored = priority_engine.rescore_all(db)
    log(db, "weather_rule_deactivated", current_user.user_id, {"rule_id": rule_id, "tasks_rescored": rescored})
    return {"rule_id": rule_id, "active": False, "tasks_rescored": rescored}


@router.get("/audit-log")
def get_audit_log(
    limit: int = 200,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN", "COA")),
):
    rows = (
        db.query(models.AuditLog)
        .order_by(models.AuditLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "log_id": r.log_id,
            "action": r.action,
            "user_id": r.user_id,
            "timestamp": r.timestamp,
            "details": json.loads(r.details or "{}"),
        }
        for r in rows
    ]


# Every audit action a manual override (FR-COA-03) can produce — see
# routers/schedule.py's manual-assign/-unschedule/-swap/reschedule endpoints.
# Kept as one list here so the admin "Manual Overrides" view and this
# summary endpoint agree on exactly what counts as an override action.
OVERRIDE_AUDIT_ACTIONS = [
    "block_manually_assigned", "block_manually_unscheduled", "blocks_manually_swapped", "block_manually_rescheduled",
]


@router.get("/overrides")
def get_overrides_summary(
    horizon: str = "weekly",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN", "COA")),
):
    """Same "elevated visibility, not elevated write access" pattern as the
    audit log — a dedicated, pre-filtered view of exactly the manual-override
    activity (not the whole audit trail) plus how much of the CURRENT plan
    is manually placed vs optimizer-generated, for the admin dashboard's
    "Manual Overrides" section."""
    plan = (
        db.query(models.BlockPlan)
        .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
        .order_by(models.BlockPlan.version.desc())
        .first()
    )
    audit_rows = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.action.in_(OVERRIDE_AUDIT_ACTIONS))
        .order_by(models.AuditLog.timestamp.desc())
        .limit(200)
        .all()
    )
    audit_trail = [
        {"log_id": r.log_id, "action": r.action, "user_id": r.user_id, "timestamp": r.timestamp, "details": json.loads(r.details or "{}")}
        for r in audit_rows
    ]

    if not plan:
        return {
            "horizon": horizon, "plan_id": None, "plan_status": None, "modified_after_publication": False,
            "total_entries": 0, "override_count": 0, "override_pct": 0.0, "overrides": [], "audit_trail": audit_trail,
        }

    entries = db.query(models.BlockPlanEntry).filter_by(plan_id=plan.plan_id).all()
    overridden = [e for e in entries if e.override]
    override_pct = round(len(overridden) / len(entries) * 100, 1) if entries else 0.0

    return {
        "horizon": horizon,
        "plan_id": plan.plan_id,
        "plan_status": plan.status,
        "modified_after_publication": plan.modified_after_publication,
        "total_entries": len(entries),
        "override_count": len(overridden),
        "override_pct": override_pct,
        "overrides": [
            {
                "id": e.id, "task_id": e.task_id, "corridor_id": e.corridor_id, "department": e.department,
                "assigned_window_start": e.assigned_window_start, "assigned_window_end": e.assigned_window_end,
                "override_reason": e.override_reason, "override_by": e.override_by, "override_at": e.override_at,
            }
            for e in overridden
        ],
        "audit_trail": audit_trail,
    }


# ============================================================ Feature 10: crew & resource capacity

def _capacity_row_out(r: models.DepartmentCapacity) -> dict:
    return {
        "id": r.id, "department": r.department, "date": r.date.isoformat() if r.date else None,
        "is_default": r.date is None, "max_concurrent_gangs": r.max_concurrent_gangs, "notes": r.notes or "",
        "updated_by": r.updated_by, "updated_at": r.updated_at,
    }


def _proposed_crew(db: Session, department: str, gangs: int, date, replacing_row: models.DepartmentCapacity = None):
    """The CrewCapacity that WOULD be in force after a change, for the
    "reducing to N gangs would make M tasks infeasible" warning."""
    proposed = crew_capacity.CrewCapacity.load(db).copy()
    if replacing_row is not None and replacing_row.date is not None:
        proposed.overrides.pop((replacing_row.department, replacing_row.date), None)
    if date is None:
        proposed.defaults[department] = gangs
    else:
        proposed.overrides[(department, date)] = (gangs, "")
    return proposed


def _impact_payload(db: Session, department: str, gangs: int, date, replacing_row=None) -> dict:
    impact = crew_capacity.capacity_change_impact(db, department, _proposed_crew(db, department, gangs, date, replacing_row))
    impact["warning"] = crew_capacity.impact_message(department, gangs, date, impact)
    return impact


@router.get("/department-capacity")
def list_department_capacity(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_role("ADMIN", "COA"))):
    """COA can read (it needs to see why a task was crew-limited); only
    ADMIN can change capacity — same visibility pattern as weather rules."""
    crew_capacity.ensure_default_capacity(db)
    rows = (
        db.query(models.DepartmentCapacity)
        .order_by(models.DepartmentCapacity.department, models.DepartmentCapacity.date)
        .all()
    )
    return {
        "departments": crew_capacity.DEPARTMENTS,
        "department_names": crew_capacity.DEPARTMENT_NAMES,
        "rows": [_capacity_row_out(r) for r in rows],
        "can_change": current_user.role == "ADMIN",
    }


@router.get("/department-capacity/impact")
def department_capacity_impact(
    department: str,
    max_concurrent_gangs: int,
    date: dt.date = None,
    replacing_id: int = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN", "COA")),
):
    """Dry run: how many currently-scheduled tasks a proposed capacity would
    make infeasible, per active plan. Writes nothing."""
    department = department.upper()
    if department not in crew_capacity.DEPARTMENTS:
        raise HTTPException(status_code=400, detail=f"department must be one of {crew_capacity.DEPARTMENTS}")
    replacing = db.query(models.DepartmentCapacity).filter_by(id=replacing_id).first() if replacing_id else None
    return _impact_payload(db, department, max_concurrent_gangs, date, replacing)


@router.post("/department-capacity")
def create_department_capacity(
    payload: schemas.DepartmentCapacityCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN")),
):
    department = payload.department.upper()
    if department not in crew_capacity.DEPARTMENTS:
        raise HTTPException(status_code=400, detail=f"department must be one of {crew_capacity.DEPARTMENTS}")
    crew_capacity.ensure_default_capacity(db)
    q = db.query(models.DepartmentCapacity).filter_by(department=department)
    q = q.filter(models.DepartmentCapacity.date.is_(None)) if payload.date is None else q.filter_by(date=payload.date)
    existing = q.first()
    if existing:
        which = "default" if payload.date is None else payload.date.isoformat()
        raise HTTPException(
            status_code=409,
            detail=f"{department} already has a {which} capacity row (id {existing.id}) — "
                   f"PATCH /api/admin/department-capacity/{existing.id} to change it",
        )

    impact = _impact_payload(db, department, payload.max_concurrent_gangs, payload.date)
    row = models.DepartmentCapacity(
        department=department, date=payload.date, max_concurrent_gangs=payload.max_concurrent_gangs,
        notes=payload.notes or "", updated_by=current_user.user_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log(db, "department_capacity_created", current_user.user_id, {
        "id": row.id, "department": department, "date": payload.date.isoformat() if payload.date else None,
        "max_concurrent_gangs": row.max_concurrent_gangs, "notes": row.notes,
        "tasks_newly_infeasible": impact["tasks_newly_infeasible_count"],
    })
    return {**_capacity_row_out(row), "impact": impact}


@router.patch("/department-capacity/{row_id}")
def update_department_capacity(
    row_id: int,
    payload: schemas.DepartmentCapacityUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN")),
):
    row = db.query(models.DepartmentCapacity).filter_by(id=row_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="capacity row not found")

    fields = payload.model_fields_set
    new_date = row.date
    if "date" in fields:
        if row.date is None and payload.date is not None:
            raise HTTPException(status_code=400, detail="the department default row cannot become a date override — create a new override instead")
        if row.date is not None and payload.date is None:
            raise HTTPException(status_code=400, detail="a date override must keep a date — delete it to fall back to the default")
        new_date = payload.date
        if new_date != row.date and db.query(models.DepartmentCapacity).filter_by(department=row.department, date=new_date).first():
            raise HTTPException(status_code=409, detail=f"{row.department} already has an override for {new_date.isoformat()}")
    new_gangs = payload.max_concurrent_gangs if payload.max_concurrent_gangs is not None else row.max_concurrent_gangs

    before = _capacity_row_out(row)
    impact = _impact_payload(db, row.department, new_gangs, new_date, replacing_row=row)
    row.date = new_date
    row.max_concurrent_gangs = new_gangs
    if payload.notes is not None:
        row.notes = payload.notes
    row.updated_by = current_user.user_id
    db.commit()
    db.refresh(row)
    after = _capacity_row_out(row)
    log(db, "department_capacity_updated", current_user.user_id, {
        "id": row.id, "department": row.department,
        "before": {k: before[k] for k in ("date", "max_concurrent_gangs", "notes")},
        "after": {k: after[k] for k in ("date", "max_concurrent_gangs", "notes")},
        "tasks_newly_infeasible": impact["tasks_newly_infeasible_count"],
    })
    return {**after, "impact": impact}


@router.delete("/department-capacity/{row_id}")
def delete_department_capacity(
    row_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("ADMIN")),
):
    row = db.query(models.DepartmentCapacity).filter_by(id=row_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="capacity row not found")
    if row.date is None:
        raise HTTPException(status_code=400, detail="a department default can be edited but not deleted")
    before = _capacity_row_out(row)
    db.delete(row)
    db.commit()
    log(db, "department_capacity_deleted", current_user.user_id, {
        "id": row_id, "department": before["department"], "date": before["date"],
        "max_concurrent_gangs": before["max_concurrent_gangs"], "notes": before["notes"],
    })
    return {"deleted": row_id, "department": before["department"], "date": before["date"]}
