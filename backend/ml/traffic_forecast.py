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
import weather_service
from tz_utils import ist_date_to_utc_bounds, ist_today, to_ist

HOURS_OF_HISTORY = 24 * 7  # one real week of the recurring daily schedule
HORIZON_DAYS = {"weekly": 7, "monthly": 30}

# Weather-aware low-traffic-window adjustment (Layer 3B): the SARIMA forecast
# above models train COUNT, not train PUNCTUALITY — it has no way to know
# that fog or heavy rain makes real trains run late. A late-running train
# from just before a nominally "quiet" window, or one queued to depart just
# after it, both bleed real occupancy into the edges of a window that looks
# clear on the schedule alone. Rather than trying to model delay minutes
# directly (which would need real delay-history data this project doesn't
# have — see the DATA POLICY note in README), this applies a documented,
# conservative safety margin: on a day where fog or heavy rain is forecast
# for the corridor, each low-traffic window is padded inward on both edges
# before it's offered as a candidate, and dropped entirely if that shrinks it
# below min_gap_hours. This is deliberately the same "narrow the usable
# window rather than trust the clear-weather assumption" philosophy as the
# optimizer's own soft duration buffer (see weather_service.py).
WEATHER_PADDING_MINUTES = 30
WEATHER_RAIN_PADDING_THRESHOLD_PCT = 60.0


def _hourly_counts(db: Session, corridor_id: str, horizon_start: dt.date, horizon_days: int):
    occurrences = timetable_loader.corridor_occurrences(db, corridor_id, horizon_start, horizon_days)
    # start must be the UTC instant of IST midnight on horizon_start, not
    # naive local midnight — occ["start"] values are naive-but-UTC (see
    # corridor_occurrences), so the hour-offset arithmetic below needs a
    # consistently-UTC reference point.
    start, _ = ist_date_to_utc_bounds(horizon_start)
    n_hours = horizon_days * 24
    counts = np.zeros(n_hours, dtype=float)
    for occ in occurrences:
        offset_hours = int((occ["start"] - start).total_seconds() // 3600)
        if 0 <= offset_hours < n_hours:
            counts[offset_hours] += 1
    index = [start + dt.timedelta(hours=h) for h in range(n_hours)]
    return pd.Series(counts, index=pd.DatetimeIndex(index)), occurrences


def _weather_adjust_window(window_start: dt.datetime, window_end: dt.datetime, corridor_id: str, forecast_by_date: dict):
    """Pads a candidate window inward on both edges when fog or heavy rain is
    forecast for ANY day it spans, modelling real trains running late into
    (or departing late out of) what the schedule alone says is quiet. Padding
    is capped so it never inverts a short window (start never passes the
    window's own midpoint). Returns (adjusted_start, adjusted_end, note) —
    note is None when no adjustment applied, so callers can tell a
    weather-driven change from an unaffected window without re-deriving it."""
    # forecast_by_date is keyed by IST calendar date (weather_service.
    # get_forecast_rows) — window_start/end are naive-but-UTC, so their IST
    # calendar date must go through to_ist(), not a bare .date().
    day = to_ist(window_start).date()
    hazards = []
    d = day
    while d <= to_ist(window_end).date():
        fc = forecast_by_date.get((corridor_id, d))
        if fc is not None and (fc.fog_risk or fc.precipitation_probability_pct > WEATHER_RAIN_PADDING_THRESHOLD_PCT):
            hazards.append((d, fc))
        d += dt.timedelta(days=1)
    if not hazards:
        return window_start, window_end, None

    pad = dt.timedelta(minutes=WEATHER_PADDING_MINUTES)
    midpoint = window_start + (window_end - window_start) / 2
    adj_start = min(window_start + pad, midpoint)
    adj_end = max(window_end - pad, midpoint)

    reasons = []
    for d, fc in hazards:
        if fc.fog_risk:
            reasons.append(f"fog forecast {d.isoformat()} (visibility {fc.visibility_km:.1f}km)")
        elif fc.precipitation_probability_pct > WEATHER_RAIN_PADDING_THRESHOLD_PCT:
            reasons.append(f"heavy rain forecast {d.isoformat()} ({fc.precipitation_probability_pct:.0f}%)")
    note = (
        f"Window narrowed by {WEATHER_PADDING_MINUTES}min on each edge ({', '.join(reasons)}): "
        "fog/heavy rain make real trains run late, which can bleed occupancy into a window that "
        "looks clear on the schedule alone."
    )
    return adj_start, adj_end, note


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

    today = ist_today()  # "today"/"this week" for a corridor forecast means the IST day
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
    forecast_start_utc, _ = ist_date_to_utc_bounds(forecast_start)
    forecast.index = pd.DatetimeIndex([forecast_start_utc + dt.timedelta(hours=h) for h in range(hours_to_forecast)])

    # Real trains across the forecast horizon, for bracketing low-traffic windows honestly.
    _future_series, future_occurrences = _hourly_counts(db, corridor_id, forecast_start, horizon_days)
    future_occurrences.sort(key=lambda o: o["start"])

    # Weather-aware traffic-pattern adjustment (Layer 3B) — see module
    # docstring above. One batched query, same caching shape used everywhere
    # else weather is read (weather_service.get_forecast_rows).
    forecast_by_date = weather_service.get_forecast_rows(
        db, [corridor_id], forecast_start, forecast_start + dt.timedelta(days=horizon_days)
    )

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
                adj_start, adj_end, weather_note = _weather_adjust_window(
                    window_start, window_end, corridor_id, forecast_by_date
                )
                adj_length_hours = (adj_end - adj_start).total_seconds() / 3600.0
                if adj_length_hours >= min_gap_hours:
                    preceding = next((o for o in reversed(future_occurrences) if o["end"] <= adj_start), None)
                    following = next((o for o in future_occurrences if o["start"] >= adj_end), None)
                    windows.append(
                        {
                            "start": adj_start,
                            "end": adj_end,
                            "duration_hours": round(adj_length_hours, 2),
                            "avg_forecast_trains_per_hour": round(float(forecast[i : j + 1].mean()), 3),
                            "preceding_train": preceding["train_id"] if preceding else None,
                            "following_train": following["train_id"] if following else None,
                            "weather_adjusted": weather_note is not None,
                            "weather_note": weather_note,
                            "schedule_only_duration_hours": round(length_hours, 2),
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
