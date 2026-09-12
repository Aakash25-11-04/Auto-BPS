"""Train traffic & corridor forecasting (Layer 3B) — built on REAL data, not
synthetic. The underlying series is the real, scheduled train movements
loaded from data.gov.in/DataMeet (see timetable_loader.py); this module only
adds a forecasting layer on top of it.

Approach chosen: SARIMA (statsmodels), not Prophet.
  Justification (see README "Layer 3B" for the full version): the source
  dataset has no per-day historical variation — as documented in
  timetable_loader.py, each real train segment is recorded once and treated
  as recurring identically every calendar day (the open dataset has no
  weekly running-days calendar). That means the "seasonality" that matters
  here is purely the 24-hour daily cycle, which is exactly what a SARIMA
  model with a 24-period seasonal term is built to capture directly and
  transparently via its own parameters — with no extra dependencies beyond
  statsmodels, which this project already installs. Prophet is designed for
  irregular multi-year business time series with holiday effects; it would
  be a heavier, less appropriate tool for a clean, single-cycle signal like
  this one, and its build toolchain (cmdstan) is fragile on Windows.

Because the source data recurs identically day to day, a correctly-behaving
forecast should predict "the same real pattern repeats" — which is exactly
what this produces, and is a genuine, verifiable statement about the real
schedule, not a fabricated one.
"""
import datetime as dt

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session
from statsmodels.tsa.statespace.sarimax import SARIMAX

import timetable_loader

HOURS_OF_HISTORY = 24 * 7  # one real week of the recurring daily schedule
HORIZON_DAYS = {"weekly": 7, "monthly": 30}


def _hourly_counts(db: Session, corridor_id: str, horizon_start: dt.date, horizon_days: int):
    occurrences = timetable_loader.corridor_occurrences(db, corridor_id, horizon_start, horizon_days)
    start = dt.datetime.combine(horizon_start, dt.time.min)
    n_hours = horizon_days * 24
    counts = np.zeros(n_hours, dtype=float)
    for occ in occurrences:
        offset_hours = int((occ["start"] - start).total_seconds() // 3600)
        if 0 <= offset_hours < n_hours:
            counts[offset_hours] += 1
    index = [start + dt.timedelta(hours=h) for h in range(n_hours)]
    return pd.Series(counts, index=pd.DatetimeIndex(index)), occurrences


def forecast_and_find_low_traffic_windows(db: Session, corridor_id: str, horizon: str = "weekly", min_gap_hours: float = 2, quiet_threshold: float = 0.5) -> dict:
    """Fits SARIMA on one real week of the corridor's recurring schedule,
    forecasts the requested horizon (7 or 30 days) forward, and reports
    contiguous low-traffic windows (forecasted count <= quiet_threshold
    trains/hour), each bracketed by the real trains immediately before/after
    it (read from the real schedule, not the forecast, so the bracketing
    trains are always genuine)."""
    if horizon not in HORIZON_DAYS:
        raise ValueError("horizon must be 'weekly' or 'monthly'")
    horizon_days = HORIZON_DAYS[horizon]
    hours_to_forecast = horizon_days * 24

    today = dt.date.today()
    history, _hist_occurrences = _hourly_counts(db, corridor_id, today, 7)
    if history.sum() == 0:
        raise ValueError(f"no real timetable entries found for corridor '{corridor_id}' — load the timetable first")

    model = SARIMAX(
        history, order=(1, 0, 1), seasonal_order=(1, 1, 1, 24),
        enforce_stationarity=False, enforce_invertibility=False,
    )
    fit = model.fit(disp=False)
    forecast_start = today + dt.timedelta(days=7)
    forecast = fit.get_forecast(steps=hours_to_forecast).predicted_mean
    forecast.index = pd.DatetimeIndex([dt.datetime.combine(forecast_start, dt.time.min) + dt.timedelta(hours=h) for h in range(hours_to_forecast)])

    # Real trains across the forecast horizon, for bracketing low-traffic windows honestly.
    _future_series, future_occurrences = _hourly_counts(db, corridor_id, forecast_start, horizon_days)
    future_occurrences.sort(key=lambda o: o["start"])

    is_quiet = forecast <= quiet_threshold
    windows = []
    i = 0
    vals = is_quiet.values
    idx = is_quiet.index
    while i < len(vals):
        if vals[i]:
            j = i
            while j + 1 < len(vals) and vals[j + 1]:
                j += 1
            window_start, window_end = idx[i], idx[j] + dt.timedelta(hours=1)
            length_hours = (window_end - window_start).total_seconds() / 3600.0
            if length_hours >= min_gap_hours:
                preceding = next((o for o in reversed(future_occurrences) if o["end"] <= window_start), None)
                following = next((o for o in future_occurrences if o["start"] >= window_end), None)
                windows.append(
                    {
                        "start": window_start,
                        "end": window_end,
                        "duration_hours": round(length_hours, 2),
                        "avg_forecast_trains_per_hour": round(float(forecast[i : j + 1].mean()), 3),
                        "preceding_train": preceding["train_id"] if preceding else None,
                        "following_train": following["train_id"] if following else None,
                    }
                )
            i = j + 1
        else:
            i += 1

    return {
        "corridor_id": corridor_id,
        "horizon": horizon,
        "history_real_movements": int(history.sum()),
        "forecast_horizon_start": forecast_start.isoformat(),
        "forecast_horizon_days": horizon_days,
        "low_traffic_windows_found": len(windows),
        "windows": windows,
    }
