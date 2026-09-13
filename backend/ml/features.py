"""Shared feature engineering — used identically by training (on synthetic
history) and live prediction (on a real MaintenanceTask), so a trained
model's input schema always matches what live inference builds.
"""
import datetime as dt

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from ml.synthetic_data import ASSET_TYPES, MONSOON_MONTHS
from tz_utils import ist_today

NUMERIC_FEATURES = [
    "severity", "age_years", "criticality", "overdue_days",
    "traffic_density", "historical_failure_count", "month", "is_monsoon",
]
ASSET_TYPE_DUMMY_COLUMNS = [f"asset_type_{t}" for t in ASSET_TYPES]
FEATURE_COLUMNS = NUMERIC_FEATURES + ASSET_TYPE_DUMMY_COLUMNS


def build_training_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """One-hot encodes asset_type and returns a DataFrame with exactly
    FEATURE_COLUMNS, in order, ready for model.fit(X, y)."""
    out = df.copy()
    for t in ASSET_TYPES:
        out[f"asset_type_{t}"] = (out["asset_type"] == t).astype(int)
    return out[FEATURE_COLUMNS]


def _corridor_traffic_density(db: Session, corridor_id: str) -> float:
    count = (
        db.query(func.count(models.TrainTimetableEntry.id))
        .filter(models.TrainTimetableEntry.corridor_id == corridor_id)
        .scalar()
    )
    return float(count) if count else 80.0  # neutral fallback if timetable not loaded for this corridor


def build_feature_vector_for_task(db: Session, task: models.MaintenanceTask) -> pd.DataFrame:
    """Builds the same feature row shape a trained model expects, from a
    real live task plus its registered asset master data (falling back to
    documented neutral defaults for any asset field never registered)."""
    asset = db.query(models.AssetCriticality).filter_by(asset_id=task.asset_id).first()
    age_years = asset.age_years if (asset and asset.age_years) else 10.0
    criticality = asset.criticality if asset else 3
    historical_failure_count = asset.historical_failure_count if asset else 0
    asset_type = asset.asset_type if (asset and asset.asset_type) else "rail"
    if asset_type not in ASSET_TYPES:
        asset_type = "rail"

    today = ist_today()  # monsoon-month feature has IST business meaning ("this month" for an Indian railway)
    row = {
        "severity": task.severity,
        "age_years": age_years,
        "criticality": criticality,
        "overdue_days": task.overdue_days,
        "traffic_density": _corridor_traffic_density(db, task.corridor_id),
        "historical_failure_count": historical_failure_count,
        "month": today.month,
        "is_monsoon": 1 if today.month in MONSOON_MONTHS else 0,
        "asset_type": asset_type,
    }
    df = pd.DataFrame([row])
    return build_training_matrix(df)
