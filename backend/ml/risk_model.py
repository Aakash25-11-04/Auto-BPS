"""Maintenance risk & priority prediction (Layer 3A).

Trains BOTH an XGBoost and a Random Forest model on the synthetic history
(ml/synthetic_data.py), for both targets the SRS asks for:
  - failed_within_30_days (classification) -> failure_risk_probability
  - actual_repair_duration_hours (regression) -> used as a sanity/eval signal
The better classifier by test-set AUC is persisted as the "active" risk
model; both models' metrics are kept in the evaluation report so the
comparison itself is inspectable, not just the winner.

Explainability is not optional here: every trained model gets a
feature-importance ranking, and every live prediction carries a SHAP-based
plain-English explanation of what drove that specific prediction — a score
a safety-critical decision-maker can't inspect is not a score they can use.
"""
import datetime as dt
import json
import os

import joblib
import numpy as np
import shap
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, roc_auc_score
from sklearn.model_selection import train_test_split
from sqlalchemy.orm import Session
from xgboost import XGBClassifier, XGBRegressor

import models
from ml import features
from tz_utils import utc_iso, utc_now
from ml.synthetic_data import generate_synthetic_history

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "data", "ml_models")
MODEL_DIR = os.path.normpath(MODEL_DIR)
os.makedirs(MODEL_DIR, exist_ok=True)

ACTIVE_RISK_MODEL_PATH = os.path.join(MODEL_DIR, "active_risk_model.joblib")
ACTIVE_RISK_MODEL_META_PATH = os.path.join(MODEL_DIR, "active_risk_model.meta.json")

# Blend weights for the combined ml_priority_score — documented here, not
# hidden: see README "Layer 3A" for the worked-example rationale.
ML_SCORE_WEIGHTS = {"risk": 100.0, "urgency": 0.5, "criticality": 0.5}


def _urgency_score(overdue_days: float) -> float:
    """Saturating curve (0-100), deliberately shaped differently from the
    rule-based scorer's linear-capped overdue term, so the two are a
    genuine independent second opinion rather than the same formula twice."""
    return 100.0 * (1.0 - np.exp(-overdue_days / 30.0))


def train_and_evaluate(db: Session, n_records: int = 2000, test_size: float = 0.25, seed: int = 42) -> dict:
    """Trains XGBoost + Random Forest on synthetic history for both targets,
    evaluates on a held-out test split, persists the better classifier as
    the active model, records an MLModelRun, and returns the full
    evaluation report (also what GET /api/ml/evaluation returns)."""
    df = generate_synthetic_history(n_records=n_records, seed=seed)
    X = features.build_training_matrix(df)
    y_clf = df["failed_within_30_days"]
    y_reg = df["actual_repair_duration_hours"]

    X_train, X_test, yclf_train, yclf_test, yreg_train, yreg_test = train_test_split(
        X, y_clf, y_reg, test_size=test_size, random_state=seed, stratify=y_clf
    )

    # utc_iso(), not a bare .isoformat() call on a naive datetime — this
    # field is a pre-built string, so it bypasses the app-wide JSON encoder
    # fix (tz_utils.install_global_json_encoder) that adds an explicit
    # offset to actual datetime objects; utc_iso() gets the same explicit
    # "+00:00" here directly.
    report = {"trained_at": utc_iso(utc_now()), "n_records": n_records, "test_size": test_size, "trained_on": "synthetic"}

    # ---- classification: failure risk ----
    xgb_clf = XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.08, eval_metric="logloss", random_state=seed,
    )
    xgb_clf.fit(X_train, yclf_train)
    xgb_auc = roc_auc_score(yclf_test, xgb_clf.predict_proba(X_test)[:, 1])

    rf_clf = RandomForestClassifier(n_estimators=300, max_depth=8, random_state=seed)
    rf_clf.fit(X_train, yclf_train)
    rf_auc = roc_auc_score(yclf_test, rf_clf.predict_proba(X_test)[:, 1])

    winner_name, winner_model, winner_auc = ("xgboost", xgb_clf, xgb_auc) if xgb_auc >= rf_auc else ("random_forest", rf_clf, rf_auc)

    report["classification"] = {
        "target": "failed_within_30_days",
        "xgboost_auc": round(float(xgb_auc), 4),
        "random_forest_auc": round(float(rf_auc), 4),
        "chosen_model": winner_name,
        "chosen_model_auc": round(float(winner_auc), 4),
    }

    # ---- regression: repair duration (sanity signal + RMSE/MAE reporting) ----
    xgb_reg = XGBRegressor(n_estimators=200, max_depth=4, learning_rate=0.08, random_state=seed)
    xgb_reg.fit(X_train, yreg_train)
    xgb_pred = xgb_reg.predict(X_test)
    xgb_rmse = float(np.sqrt(mean_squared_error(yreg_test, xgb_pred)))
    xgb_mae = float(mean_absolute_error(yreg_test, xgb_pred))

    rf_reg = RandomForestRegressor(n_estimators=300, max_depth=8, random_state=seed)
    rf_reg.fit(X_train, yreg_train)
    rf_pred = rf_reg.predict(X_test)
    rf_rmse = float(np.sqrt(mean_squared_error(yreg_test, rf_pred)))
    rf_mae = float(mean_absolute_error(yreg_test, rf_pred))

    report["regression"] = {
        "target": "actual_repair_duration_hours",
        "xgboost_rmse": round(xgb_rmse, 3), "xgboost_mae": round(xgb_mae, 3),
        "random_forest_rmse": round(rf_rmse, 3), "random_forest_mae": round(rf_mae, 3),
    }

    # ---- feature importances (the chosen classifier) ----
    importances = dict(zip(features.FEATURE_COLUMNS, [float(v) for v in winner_model.feature_importances_]))
    top_features = sorted(importances.items(), key=lambda kv: kv[1], reverse=True)
    report["feature_importances"] = [{"feature": f, "importance": round(v, 4)} for f, v in top_features]

    # ---- rule-vs-ML ranking agreement, computed against synthetic ground truth ----
    # true_risk_probability is the GENERATING probability (not the noisy
    # binary label) — comparing the model's predicted ranking against it is
    # the honest way to check whether the model recovered the underlying
    # signal or just memorized noise.
    model_probs = winner_model.predict_proba(X_test)[:, 1]
    true_probs = df.loc[X_test.index, "true_risk_probability"].values
    rank_corr = float(np.corrcoef(model_probs, true_probs)[0, 1])
    report["ranking_agreement_with_true_generating_probability"] = round(rank_corr, 4)

    # ---- honest limitations, always included, and genuinely dynamic — this
    # is an assessment of what actually happened this run, not a canned
    # assumption written before training ----
    auc = report["classification"]["chosen_model_auc"]
    if auc >= 0.85:
        auc_assessment = (
            f"AUC of {auc} is quite high — for a synthetic target this usually means the generator's "
            "causal relationships (ml/synthetic_data.py) are cleaner/more separable than real failure "
            "causation would be, so treat this as an upper bound the model would NOT reach on real data."
        )
    elif auc >= 0.65:
        auc_assessment = (
            f"AUC of {auc} is modestly better than chance (0.5), not dramatically high — the generator's "
            "injected noise (a Normal(0, 0.5) term on the risk logit, deliberately added so this task "
            "isn't trivially easy) means the model only partially recovered the true causal signal, which "
            "is a more realistic and more honest outcome than a suspiciously perfect score would be."
        )
    else:
        auc_assessment = (
            f"AUC of {auc} is close to chance (0.5) — the model is barely distinguishing the classes even "
            "on its own synthetic training distribution. That points to the synthetic signal being too "
            "noisy relative to its causal coefficients, not to any real-world predictive claim either way."
        )
    rank_assessment = (
        f"Ranking correlation of {report['ranking_agreement_with_true_generating_probability']} against the "
        "TRUE generating probability (not the noisy binary label) is the more meaningful number here: it "
        "measures whether the model recovered the underlying causal structure independent of label noise."
    )
    report["limitations"] = [
        "Trained entirely on synthetic data generated from a documented, hand-specified "
        "causal model (ml/synthetic_data.py) — it has never seen a real failure event. "
        "Its accuracy on this synthetic test set reflects how well it recovered the "
        "generating function, not real-world predictive power.",
        "Real deployment would require retraining on actual TMS/SMMS/TDMS failure "
        "history once that data is available, and re-validating every coefficient in "
        "ml/synthetic_data.py's causal model against real outcomes.",
        auc_assessment,
        rank_assessment,
    ]

    model_id = f"risk-{winner_name}-{utc_now().strftime('%Y%m%d%H%M%S')}"
    joblib.dump(winner_model, ACTIVE_RISK_MODEL_PATH)
    with open(ACTIVE_RISK_MODEL_META_PATH, "w") as f:
        json.dump({"model_id": model_id, "model_type": winner_name}, f)

    db.add(
        models.MLModelRun(
            model_id=model_id,
            model_type=f"risk_{winner_name}",
            metrics_json=json.dumps(report),
            feature_importance_json=json.dumps(report["feature_importances"]),
            notes=f"AUC {winner_auc:.4f} on {int(n_records * test_size)} held-out synthetic records",
        )
    )
    db.commit()

    report["model_id"] = model_id
    return report


def _load_active_model():
    if not os.path.exists(ACTIVE_RISK_MODEL_PATH):
        return None, None
    model = joblib.load(ACTIVE_RISK_MODEL_PATH)
    with open(ACTIVE_RISK_MODEL_META_PATH) as f:
        meta = json.load(f)
    return model, meta["model_id"]


def predict_for_task(db: Session, task: models.MaintenanceTask) -> dict:
    """Live inference for one real task. Raises RuntimeError if no model has
    been trained yet (caller decides the fallback — see priority_engine.py's
    scoring_source handling, which falls back to the rule-based score)."""
    model, model_id = _load_active_model()
    if model is None:
        raise RuntimeError("no trained risk model available — POST /api/ml/train first")

    X = features.build_feature_vector_for_task(db, task)
    risk_probability = float(model.predict_proba(X)[0, 1])
    urgency = _urgency_score(task.overdue_days)
    criticality_row = db.query(models.AssetCriticality).filter_by(asset_id=task.asset_id).first()
    criticality_score = (criticality_row.criticality if criticality_row else 3) * 20.0

    w = ML_SCORE_WEIGHTS
    ml_priority_score = w["risk"] * risk_probability + w["urgency"] * urgency + w["criticality"] * criticality_score

    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X)
    sv = shap_values[1][0] if isinstance(shap_values, list) else shap_values[0]
    contributions = sorted(zip(features.FEATURE_COLUMNS, [float(v) for v in sv]), key=lambda kv: abs(kv[1]), reverse=True)
    top = contributions[:3]
    explanation = "; ".join(
        f"{name} {'increased' if val > 0 else 'decreased'} risk by {abs(val):.3f}" for name, val in top
    )

    result = {
        "task_id": task.task_id,
        "model_id": model_id,
        "failure_risk_probability": round(risk_probability, 4),
        "urgency_score": round(urgency, 2),
        "criticality_score": round(criticality_score, 2),
        "ml_priority_score": round(ml_priority_score, 2),
        "shap_explanation": explanation,
        "trained_on": "synthetic",
    }

    existing = db.query(models.TaskPrediction).filter_by(task_id=task.task_id).first()
    if existing:
        existing.model_id, existing.failure_risk_probability = model_id, result["failure_risk_probability"]
        existing.urgency_score, existing.criticality_score = result["urgency_score"], result["criticality_score"]
        existing.ml_priority_score, existing.shap_explanation = result["ml_priority_score"], explanation
        existing.predicted_at = utc_now()
    else:
        db.add(models.TaskPrediction(task_id=task.task_id, **{k: result[k] for k in (
            "model_id", "failure_risk_probability", "urgency_score", "criticality_score", "ml_priority_score"
        )}, shap_explanation=explanation))
    db.commit()

    return result
