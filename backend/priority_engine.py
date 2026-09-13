"""Transparent, weighted rule-based priority scorer — AND the configurable
scoring-source switch (rule / ml / blend) that sits in front of it.

Rule-based formula (unchanged, still the fallback and the baseline the ML
model is compared against — see ml/risk_model.py's evaluation report):
score = w_severity * severity(1-5)
      + w_overdue   * min(overdue_days, 60)
      + w_criticality * asset_criticality(1-5)
      + w_safety_flag  if safety_critical OR interlocking_critical

Every score is accompanied by a plain-English breakdown of exactly how it was
built, so no number on the dashboard is a black box — true whether the
active scoring source is the rule, the ML model, or a blend of both.

CP-SAT NEVER sees a model directly: whichever source is active, the result
is still just a task.priority_score float feeding the optimizer's objective
exactly as before (§Layer 4/5 governance: models estimate parameters, they
never make the scheduling decision).
"""
from sqlalchemy.orm import Session

import models

DEFAULT_WEIGHTS = {
    "w_severity": 10.0,
    "w_overdue": 1.5,
    "w_criticality": 8.0,
    "w_safety_flag": 25.0,
    # Weather-aware scheduling (Layer 3A): scales every weather-driven
    # priority contribution from weather_service.weather_priority_bonus.
    # Default 1.0 means a configured rule's priority_points apply exactly as
    # tuned; set to 0 to disable weather's effect on scoring entirely without
    # touching the per-rule config.
    "w_weather_risk": 1.0,
}

OVERDUE_CAP_DAYS = 60
VALID_SCORING_SOURCES = ("rule", "ml", "blend")


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

    # Weather-aware scheduling (Layer 3A): only applies when the task's own
    # defect_type is in a configurable weather-sensitive category AND severe
    # weather is forecast on its corridor within the reliable-forecast
    # horizon — see weather_service.weather_priority_bonus. Lazy import
    # avoids a hard dependency for callers that never touch weather (mirrors
    # the ml.risk_model lazy import below).
    from weather_service import weather_priority_bonus

    weather_pts, weather_fragment = weather_priority_bonus(db, task, weights["w_weather_risk"])

    total = severity_pts + overdue_pts + criticality_pts + safety_pts + weather_pts

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
    if weather_fragment:
        justification += weather_fragment
    return total, justification


def get_scoring_source(db: Session) -> str:
    row = db.query(models.AppSetting).filter_by(key="scoring_source").first()
    return row.value if row and row.value in VALID_SCORING_SOURCES else "rule"


def set_scoring_source(db: Session, source: str) -> None:
    if source not in VALID_SCORING_SOURCES:
        raise ValueError(f"scoring_source must be one of {VALID_SCORING_SOURCES}, got '{source}'")
    row = db.query(models.AppSetting).filter_by(key="scoring_source").first()
    if row:
        row.value = source
    else:
        db.add(models.AppSetting(key="scoring_source", value=source))
    db.commit()


def compute_effective_score(db: Session, task: models.MaintenanceTask) -> tuple:
    """Returns (score, justification, source_used). source_used can differ
    from the configured source only when ML scoring was requested but no
    model has been trained yet — it falls back to 'rule_fallback' rather
    than ever raising, exactly like the scheduler's own CP-SAT-to-greedy
    fallback: a missing model is never allowed to block scoring a task."""
    rule_score, rule_reason = score_task(db, task)
    source = get_scoring_source(db)
    if source == "rule":
        return rule_score, rule_reason, "rule"

    try:
        from ml import risk_model  # lazy: avoids importing xgboost/shap when scoring_source is "rule"

        ml_result = risk_model.predict_for_task(db, task)
    except Exception as e:
        fallback_reason = rule_reason + f" [ML scoring source was requested but unavailable ({e}); used the rule-based score instead.]"
        return rule_score, fallback_reason, "rule_fallback"

    ml_score = ml_result["ml_priority_score"]
    ml_reason = (
        f"ML priority score {ml_score:.1f} (model trained on SYNTHETIC data, not real failure history): "
        f"failure risk {ml_result['failure_risk_probability'] * 100:.1f}%, urgency {ml_result['urgency_score']:.1f}, "
        f"criticality {ml_result['criticality_score']:.1f}. Top contributing factors: {ml_result['shap_explanation']}."
    )
    if source == "ml":
        return ml_score, ml_reason, "ml"

    # blend: documented, simple 50/50 average — deliberately not tuned, so the
    # blend's behavior is exactly as predictable as its two ingredients.
    blended = round(0.5 * rule_score + 0.5 * ml_score, 1)
    blended_reason = f"Blended score {blended:.1f} = 0.5×rule-based({rule_score:.1f}) + 0.5×ML({ml_score:.1f}). Rule: {rule_reason} ML: {ml_reason}"
    return blended, blended_reason, "blend"


def rescore_task(db: Session, task: models.MaintenanceTask) -> None:
    score, reason, _source_used = compute_effective_score(db, task)
    task.priority_score = score
    task.priority_reason = reason


def rescore_all(db: Session) -> int:
    """Recompute every task's score, e.g. after an admin weight change. Returns count."""
    tasks = db.query(models.MaintenanceTask).all()
    for t in tasks:
        rescore_task(db, t)
    db.commit()
    return len(tasks)
