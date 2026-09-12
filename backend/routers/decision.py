"""Layer 5 API surface: emergency re-optimization, what-if analysis,
explainability, and shadow prices. All COA-only — these are Control Office
decision-support tools, same tier as running the scheduler itself."""
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import decision_intelligence
import models
from database import get_db

router = APIRouter(prefix="/api/decision", tags=["decision"])


@router.post("/emergency-reoptimize")
def emergency_reoptimize(
    horizon: str = "weekly",
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    required = {"department", "asset_id", "defect_type", "required_duration_hours", "corridor_id"}
    missing = required - payload.keys()
    if missing:
        raise HTTPException(status_code=400, detail=f"missing required fields: {sorted(missing)}")
    if payload["department"] not in ("ENG", "TD", "SNT"):
        raise HTTPException(status_code=400, detail="department must be ENG, TD, or SNT")
    try:
        return decision_intelligence.emergency_reoptimize(db, horizon, payload, current_user.user_id)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"emergency re-optimization failed: {e}")


@router.post("/what-if")
def what_if(
    horizon: str = "weekly",
    scenarios: list = Body(...),
    current_user: models.User = Depends(auth.require_role("COA")),
    db: Session = Depends(get_db),
):
    if not scenarios or len(scenarios) < 1:
        raise HTTPException(status_code=400, detail="provide at least one scenario config")
    return decision_intelligence.run_what_if(db, horizon, scenarios)


@router.get("/explain/{task_id}")
def explain(
    task_id: str,
    horizon: str = "weekly",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    task = db.query(models.MaintenanceTask).filter_by(task_id=task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if current_user.role in ("ENG", "TD", "SNT") and current_user.department != task.department:
        raise HTTPException(status_code=403, detail="you may not view another department's task explanation")
    try:
        return decision_intelligence.explain_task(db, horizon, task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/shadow-price")
def shadow_price(
    corridor_id: str,
    horizon: str = "weekly",
    extra_hours: float = 2.0,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    return decision_intelligence.compute_shadow_price(db, horizon, corridor_id, extra_hours)
