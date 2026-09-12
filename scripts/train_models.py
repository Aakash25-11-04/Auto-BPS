#!/usr/bin/env python
"""Standalone CLI for training the Layer 3 risk models — the same code path
POST /api/ml/train uses, runnable without the server for offline experimentation.

Usage (from the project root):
    python scripts/train_models.py [n_records]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from database import Base, SessionLocal, engine  # noqa: E402
from ml.risk_model import train_and_evaluate  # noqa: E402

if __name__ == "__main__":
    n_records = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        report = train_and_evaluate(db, n_records=n_records)
    finally:
        db.close()

    print(f"\nModel: {report['model_id']}")
    print(f"Classification AUC: XGBoost={report['classification']['xgboost_auc']}  "
          f"RandomForest={report['classification']['random_forest_auc']}  "
          f"chosen={report['classification']['chosen_model']}")
    print(f"Regression (duration): XGBoost RMSE={report['regression']['xgboost_rmse']}h  "
          f"RandomForest RMSE={report['regression']['random_forest_rmse']}h")
    print(f"Ranking agreement with true generating probability: "
          f"{report['ranking_agreement_with_true_generating_probability']}")
    print("\nTop feature importances:")
    for f in report["feature_importances"][:5]:
        print(f"  {f['feature']:<28} {f['importance']}")
    print("\nLimitations:")
    for line in report["limitations"]:
        print(f"  - {line}")
