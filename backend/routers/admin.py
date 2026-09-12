"""Administrator endpoints: users, scoring weights, asset criticality, audit log.

RBAC: everything under /api/admin/* requires ADMIN — except GET
/api/admin/audit-log, which is explicitly ADMIN-and-COA (the Control Office
needs visibility into the operational audit trail — schedule runs,
approvals, rejections — without holding full ADMIN system-configuration
rights). That one route is the single, deliberate, explicitly-stated
exception to the blanket ADMIN-only rule; every other route here uses
require_role("ADMIN") alone.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import models
import priority_engine
import schemas
from audit import log
from database import get_db

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users")
def list_users(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_role("ADMIN"))):
    rows = db.query(models.User).all()
    # password_hash is intentionally never included in this response.
    return [
        {"user_id": u.user_id, "name": u.name, "role": u.role, "department": u.department, "active": u.active}
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
