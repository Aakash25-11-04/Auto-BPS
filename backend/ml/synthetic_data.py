"""Synthetic historical maintenance data generator.

Real TMS/SMMS/TDMS failure history is an internal railway dataset ABPS has
no access to (same policy as the live task data — see README's data-honesty
section). Training a real risk model needs historical examples of "this
asset, in this condition, did or didn't fail," which simply doesn't exist
publicly. This module generates that history synthetically, with a
DOCUMENTED, CAUSAL structure, so the model learns a genuine (if invented)
relationship rather than fitting noise. Every model trained on this data is
labelled "trained on synthetic data" everywhere it surfaces in the API and UI
— it is never presented as if it came from real operational history.

Documented causal structure (the ground truth this data is built from):
  - failure risk RISES with: severity, asset age, asset criticality, overdue
    days, corridor traffic density, and prior failure count.
  - failure risk has a MONSOON SEASONALITY bump (June-September) — Indian
    Railways track/OHE assets are genuinely more failure-prone in the
    monsoon, and modelling that dependency is what makes "seasonality" a
    meaningful input rather than a decorative one.
  - repair duration depends on defect TYPE (a signal relay swap is quick; a
    rail fracture repair is slow) plus a smaller severity effect.
  - every relationship above is a real, inspectable line in this file, not
    a hidden parameter — see _RISK_LOGIT_COEFFICIENTS.
"""
import datetime as dt
import math
import random

import numpy as np
import pandas as pd

RANDOM_SEED = 42

ASSET_TYPES = ["rail", "ohe_insulator", "signal_relay", "point_machine", "ballast"]
DEFECT_TYPE_BY_ASSET = {
    "rail": "rail_fracture",
    "ohe_insulator": "ohe_insulator_fault",
    "signal_relay": "signal_relay_fault",
    "point_machine": "point_machine_test",
    "ballast": "ballast_degradation",
}
BASE_REPAIR_HOURS = {
    "rail_fracture": 6.0,
    "ohe_insulator_fault": 4.0,
    "signal_relay_fault": 3.0,
    "point_machine_test": 2.0,
    "ballast_degradation": 5.0,
}
MONSOON_MONTHS = {6, 7, 8, 9}

# The exact, documented causal weights behind the synthetic failure-risk
# label. Changing these changes what the model can genuinely learn — they
# are intentionally simple, monotonic, and in the direction domain
# knowledge would predict.
_RISK_LOGIT_COEFFICIENTS = {
    "intercept": -3.4,
    "severity": 0.38,
    "age_years": 0.028,
    "criticality": 0.26,
    "overdue_days": 0.018,
    "traffic_density_per_10": 0.09,
    "historical_failure_count": 0.22,
    "monsoon_bonus": 0.45,
}


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def generate_synthetic_history(n_records: int = 2000, seed: int = RANDOM_SEED) -> pd.DataFrame:
    """Returns a DataFrame of n_records synthetic historical maintenance
    events with two targets: failed_within_30_days (classification) and
    actual_repair_duration_hours (regression). Deterministic given `seed`."""
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    rows = []
    for i in range(n_records):
        asset_type = rng.choice(ASSET_TYPES)
        defect_type = DEFECT_TYPE_BY_ASSET[asset_type]
        severity = rng.choices([1, 2, 3, 4, 5], weights=[10, 20, 30, 25, 15])[0]
        age_years = round(np_rng.uniform(0, 30), 1)
        criticality = rng.choices([1, 2, 3, 4, 5], weights=[10, 15, 30, 25, 20])[0]
        overdue_days = max(0, int(np_rng.exponential(scale=15)))
        overdue_days = min(overdue_days, 90)
        traffic_density = round(np_rng.uniform(20, 150), 1)  # real trains/week range observed on this corridor set
        historical_failure_count = np_rng.poisson(lam=max(0.2, age_years / 12.0))
        month = rng.randint(1, 12)

        monsoon = 1 if month in MONSOON_MONTHS else 0
        c = _RISK_LOGIT_COEFFICIENTS
        logit = (
            c["intercept"]
            + c["severity"] * severity
            + c["age_years"] * age_years
            + c["criticality"] * criticality
            + c["overdue_days"] * overdue_days
            + c["traffic_density_per_10"] * (traffic_density / 10.0)
            + c["historical_failure_count"] * historical_failure_count
            + c["monsoon_bonus"] * monsoon
            + np_rng.normal(0, 0.5)  # irreducible noise — real failures aren't perfectly deterministic either
        )
        risk_probability = _sigmoid(logit)
        failed_within_30_days = 1 if np_rng.uniform(0, 1) < risk_probability else 0

        duration = (
            BASE_REPAIR_HOURS[defect_type]
            + 0.3 * severity
            + np_rng.normal(0, 0.6)
        )
        duration = max(0.5, round(duration, 2))

        rows.append(
            {
                "asset_type": asset_type,
                "defect_type": defect_type,
                "severity": severity,
                "age_years": age_years,
                "criticality": criticality,
                "overdue_days": overdue_days,
                "traffic_density": traffic_density,
                "historical_failure_count": int(historical_failure_count),
                "month": month,
                "is_monsoon": monsoon,
                "true_risk_probability": round(risk_probability, 4),  # kept for evaluation only, not a training feature
                "failed_within_30_days": failed_within_30_days,
                "actual_repair_duration_hours": duration,
            }
        )

    return pd.DataFrame(rows)
