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
    else:
        db.add(models.AssetCriticality(asset_id=payload.asset_id, criticality=payload.criticality))
    db.commit()

    affected = db.query(models.MaintenanceTask).filter_by(asset_id=payload.asset_id).all()
    for t in affected:
        priority_engine.rescore_task(db, t)
    db.commit()

    log(db, "asset_criticality_set", current_user.user_id, {"asset_id": payload.asset_id, "criticality": payload.criticality, "tasks_rescored": len(affected)})
    return {"asset_id": payload.asset_id, "criticality": payload.criticality, "tasks_rescored": len(affected)}


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
