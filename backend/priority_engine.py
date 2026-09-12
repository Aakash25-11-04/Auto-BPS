"""Transparent, weighted rule-based priority scorer.

score = w_severity * severity(1-5)
      + w_overdue   * min(overdue_days, 60)
      + w_criticality * asset_criticality(1-5)
      + w_safety_flag  if safety_critical OR interlocking_critical

Every score is accompanied by a plain-English breakdown of exactly how it was
built, so no number on the dashboard is a black box.
"""
from sqlalchemy.orm import Session

import models

DEFAULT_WEIGHTS = {
    "w_severity": 10.0,
    "w_overdue": 1.5,
    "w_criticality": 8.0,
    "w_safety_flag": 25.0,
}

OVERDUE_CAP_DAYS = 60


def get_weights(db: Session) -> dict:
    weights = dict(DEFAULT_WEIGHTS)
    for row in db.query(models.ScoringConfig).all():
        if row.key in weights:
            weights[row.key] = row.value
    return weights


def get_asset_criticality(db: Session, asset_id: str) -> int:
    row = db.query(models.AssetCriticality).filter_by(asset_id=asset_id).first()
    return row.criticality if row else 3  # neutral default when asset is unregistered


def score_task(db: Session, task: models.MaintenanceTask) -> tuple:
    """Returns (score, justification_text)."""
    weights = get_weights(db)
    criticality = get_asset_criticality(db, task.asset_id)
    capped_overdue = min(task.overdue_days, OVERDUE_CAP_DAYS)

    severity_pts = weights["w_severity"] * task.severity
    overdue_pts = weights["w_overdue"] * capped_overdue
    criticality_pts = weights["w_criticality"] * criticality
    safety_flag = task.safety_critical or task.interlocking_critical
    safety_pts = weights["w_safety_flag"] if safety_flag else 0.0

    total = severity_pts + overdue_pts + criticality_pts + safety_pts

    parts = [
        f"severity {task.severity}/5 contributed {severity_pts:.1f} pts",
        f"{capped_overdue} overdue day(s) contributed {overdue_pts:.1f} pts",
        f"asset criticality {criticality}/5 contributed {criticality_pts:.1f} pts",
    ]
    if task.overdue_days > OVERDUE_CAP_DAYS:
        parts[1] += f" (capped from {task.overdue_days})"
    if safety_flag:
        flag_names = []
        if task.safety_critical:
            flag_names.append("safety-critical")
        if task.interlocking_critical:
            flag_names.append("interlocking-critical")
        parts.append(f"{'/'.join(flag_names)} flag contributed {safety_pts:.1f} pts")

    justification = f"Priority score {total:.1f}: " + "; ".join(parts) + "."
    return total, justification


def rescore_task(db: Session, task: models.MaintenanceTask) -> None:
    score, reason = score_task(db, task)
    task.priority_score = score
    task.priority_reason = reason


def rescore_all(db: Session) -> int:
    """Recompute every task's score, e.g. after an admin weight change. Returns count."""
    tasks = db.query(models.MaintenanceTask).all()
    for t in tasks:
        rescore_task(db, t)
    db.commit()
    return len(tasks)
