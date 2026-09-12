"""Layer 3 API surface: model training/evaluation and live prediction.

RBAC: training a model is a system-configuration-grade action (COA/ADMIN,
same tier as loading the timetable); reading the evaluation report or a
task's prediction is open to any authenticated user (mirrors task-level
visibility rules — a department can see its own tasks' predictions).
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import models
from audit import log
from database import get_db
from ml import risk_model, traffic_forecast

router = APIRouter(prefix="/api/ml", tags=["ml"])

DEPT_ROLES = ("ENG", "TD", "SNT")


@router.post("/train")
def train_models(
    n_records: int = 2000,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA", "ADMIN")),
):
    report = risk_model.train_and_evaluate(db, n_records=n_records)
    log(db, "ml_model_trained", current_user.user_id, {"model_id": report["model_id"], "classification": report["classification"]})
    return report


@router.get("/evaluation")
def get_latest_evaluation(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    run = db.query(models.MLModelRun).order_by(models.MLModelRun.trained_at.desc()).first()
    if not run:
        raise HTTPException(status_code=404, detail="no model has been trained yet — POST /api/ml/train first")
    return json.loads(run.metrics_json or "{}")


@router.get("/predict/{task_id}")
def predict_task(task_id: str, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    task = db.query(models.MaintenanceTask).filter_by(task_id=task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if current_user.role in DEPT_ROLES and current_user.department != task.department:
        raise HTTPException(status_code=403, detail="you may not view another department's task prediction")
    try:
        return risk_model.predict_for_task(db, task)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/traffic-forecast")
def get_traffic_forecast(
    corridor_id: str,
    horizon: str = "weekly",
    min_gap_hours: float = 2.0,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    try:
        return traffic_forecast.forecast_and_find_low_traffic_windows(db, corridor_id, horizon=horizon, min_gap_hours=min_gap_hours)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
