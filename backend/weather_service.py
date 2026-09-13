"""Weather-aware scheduling — Layers 2/3/4 glue.

Layer 2 (integration): ingests forecasts from the pluggable WeatherAdapter
(backend/adapters/weather.py) into WeatherForecastEntry, resolved to the
corridor's nearest real station and validated (rejects impossible values).

Layer 3 (analytics): weather_priority_bonus() feeds a transparent, explained
points contribution into priority_engine.score_task() exactly the way every
other scoring input works — never a silent multiplier.

Layer 4 (optimizer): assess_candidates() is what scheduler.py calls before
building CP-SAT/greedy candidates. It returns, per (task, candidate window):
  - whether the window must be HARD-EXCLUDED (safety-critical outdoor/height/
    live-equipment work vs. lightning/high-wind/extreme-heat forecast) —
    the task cannot be assigned there at all;
  - the EFFECTIVE duration the solver should use for that window (base
    duration inflated by a configurable % when rain/fog risk is elevated but
    below the hard threshold), so the solver doesn't overpack a window that
    will realistically run long.

Forecast reliability by horizon (the DATA POLICY constraint): weather
forecasts are only meaningfully reliable ~5-7 days out. RELIABLE_HORIZON_DAYS
gates the HARD exclusion only — a candidate window more than that many days
from today can never be hard-excluded on a forecast basis, regardless of
what any rule says. The WEEKLY horizon (7 days) is therefore hard-excluded
+ soft-buffered on every candidate by construction; the MONTHLY horizon
(30 days) is hard-excluded only for its first ~7 days and soft-buffered/
priority-nudged (never hard-excluded) for the rest — exactly the distinction
the spec requires, and it falls directly out of one date comparison rather
than a second, separately-tracked "horizon strictness" flag.
"""
import datetime as dt
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

import models
from adapters.weather import WEATHER_ADAPTERS, WeatherProviderUnavailable
from audit import log
from tz_utils import IST, UTC, ist_date_to_utc_bounds, ist_iso, ist_today, to_ist, utc_iso, utc_now

RELIABLE_HORIZON_DAYS = 7  # forecasts beyond this are advisory-only, never a hard exclusion
FORECAST_TTL_HOURS = 6  # don't re-fetch a corridor's forecast more often than this


def _validate_hourly_row(row: dict) -> None:
    """Rejects physically impossible values rather than storing them. A
    provider field that is simply absent (None) is allowed through — an
    unknown value is honest; an invented one would not be."""
    p = row.get("precipitation_probability_pct")
    if p is not None and not (0 <= p <= 100):
        raise ValueError(f"precipitation_probability_pct out of range: {p}")
    w = row.get("wind_speed_kmh")
    if w is not None and w < 0:
        raise ValueError(f"wind_speed_kmh cannot be negative: {w}")
    v = row.get("visibility_km")
    if v is not None and v < 0:
        raise ValueError(f"visibility_km cannot be negative: {v}")
    t = row.get("temperature_c")
    if t is not None and not (-60 <= t <= 60):
        raise ValueError(f"temperature out of physically plausible range: {t}")

VALID_HAZARDS = ("lightning", "wind", "heat", "rain", "fog")
VALID_MODES = ("hard", "soft", "priority")

# Seeded once, on first use, into WeatherSensitivityRule — NOT hardcoded into
# the scheduler or priority engine. An Administrator can edit, add, or
# deactivate any of these via /api/admin/weather-rules exactly like the
# scoring weights, and the CHANGE takes effect on the next scheduler run /
# rescore with no code change.
DEFAULT_RULES = [
    # ---- hard: safety-critical outdoor/height/live-equipment exclusions ----
    dict(rule_id="hard-lightning-ohe", label="Lightning risk excludes OHE/traction work",
         defect_type_pattern="ohe", hazard="lightning", mode="hard", threshold=0),
    dict(rule_id="hard-lightning-signal", label="Lightning risk excludes signal/point-machine work at height",
         defect_type_pattern="signal", hazard="lightning", mode="hard", threshold=0),
    dict(rule_id="hard-lightning-point-machine", label="Lightning risk excludes point-machine work",
         defect_type_pattern="point_machine", hazard="lightning", mode="hard", threshold=0),
    dict(rule_id="hard-wind-ohe", label="High wind excludes OHE/height work",
         defect_type_pattern="ohe", hazard="wind", mode="hard", threshold=50.0),
    dict(rule_id="hard-wind-feeder", label="High wind excludes overhead feeder-cable work",
         defect_type_pattern="feeder", hazard="wind", mode="hard", threshold=50.0),
    dict(rule_id="hard-heat-rail", label="Extreme heat excludes rail work (buckling risk)",
         defect_type_pattern="rail", hazard="heat", mode="hard", threshold=45.0),
    dict(rule_id="hard-heat-ballast", label="Extreme heat excludes ballast/track work (worker heat stress)",
         defect_type_pattern="ballast", hazard="heat", mode="hard", threshold=45.0),
    # ---- soft: duration buffer, not exclusion ----
    dict(rule_id="soft-rain-track", label="Elevated rain probability slows track/ballast work",
         defect_type_pattern="rail", hazard="rain", mode="soft", threshold=60.0, duration_buffer_pct=20.0),
    dict(rule_id="soft-rain-ballast", label="Elevated rain probability slows ballast work",
         defect_type_pattern="ballast", hazard="rain", mode="soft", threshold=60.0, duration_buffer_pct=20.0),
    dict(rule_id="soft-rain-signal", label="Elevated rain probability slows signal/telecom work",
         defect_type_pattern="signal", hazard="rain", mode="soft", threshold=60.0, duration_buffer_pct=15.0),
    dict(rule_id="soft-fog-signal", label="Fog slows signal/telecom work (visibility)",
         defect_type_pattern="signal", hazard="fog", mode="soft", threshold=1.0, duration_buffer_pct=15.0),
    # ---- priority: forecast-driven urgency bump ----
    dict(rule_id="priority-rain-drainage", label="Heavy rain forecast raises drainage-defect urgency",
         defect_type_pattern="drainage", hazard="rain", mode="priority", threshold=50.0, priority_points=15.0),
    dict(rule_id="priority-rain-embankment", label="Heavy rain forecast raises embankment-defect urgency",
         defect_type_pattern="embankment", hazard="rain", mode="priority", threshold=50.0, priority_points=15.0),
    dict(rule_id="priority-rain-ohe-corrosion", label="Heavy rain forecast raises corrosion-prone OHE urgency",
         defect_type_pattern="ohe_insulator", hazard="rain", mode="priority", threshold=50.0, priority_points=10.0),
    dict(rule_id="priority-rain-corrosion", label="Heavy rain forecast raises corrosion-flagged-asset urgency",
         defect_type_pattern="corrosion", hazard="rain", mode="priority", threshold=50.0, priority_points=10.0),
]


def ensure_default_rules(db: Session) -> int:
    if db.query(models.WeatherSensitivityRule).first():
        return 0
    for r in DEFAULT_RULES:
        db.add(models.WeatherSensitivityRule(**r))
    db.commit()
    return len(DEFAULT_RULES)


def get_rules(db: Session, mode: str = None) -> List[models.WeatherSensitivityRule]:
    ensure_default_rules(db)
    q = db.query(models.WeatherSensitivityRule).filter_by(active=True)
    if mode:
        q = q.filter_by(mode=mode)
    return q.all()


def _matches(rule: models.WeatherSensitivityRule, defect_type: str) -> bool:
    pattern = (rule.defect_type_pattern or "").strip().lower()
    if pattern in ("*", ""):
        return True
    return pattern in (defect_type or "").lower()


# ============================================================ IST calendar-date <-> UTC storage

def _ist_midnight_to_utc(ist_date: dt.date) -> dt.datetime:
    """The UTC instant this system stores for 'the forecast covering IST
    calendar date D' — IST midnight of D, converted to UTC. Naive-but-UTC,
    matching every other stored datetime (see tz_utils.py)."""
    return ist_date_to_utc_bounds(ist_date)[0]


def forecast_date_ist(row: models.WeatherForecastEntry) -> dt.date:
    """The IST calendar date a stored WeatherForecastEntry row is FOR.
    forecast_date is stored as the UTC-equivalent of IST midnight (see
    _ist_midnight_to_utc) — taking .date() directly on the raw stored value
    would silently give the wrong day (a UTC offset can move midnight
    across a calendar boundary), so this is the only correct way to read
    it back."""
    return to_ist(row.forecast_date).date()


# ============================================================ Layer 1/2: ingestion

def corridor_location(db: Session, corridor_id: str) -> Optional[dict]:
    """The real lat/lon a corridor's weather is fetched for: the MIDPOINT of
    its endpoint stations' real coordinates when both are known (a corridor
    is a stretch of line, not a point, so its midpoint represents it better
    than either end), falling back to whichever endpoint has coordinates.
    All coordinates come from the real station master (station_loader.py) —
    never invented."""
    corridor = db.query(models.Corridor).filter_by(corridor_id=corridor_id).first()
    if corridor:
        from_code, to_code = corridor.from_station_code, corridor.to_station_code
    else:
        parts = (corridor_id or "").split("-")
        if len(parts) != 2:
            return None
        from_code, to_code = parts

    a = db.query(models.Station).filter_by(station_code=from_code).first()
    b = db.query(models.Station).filter_by(station_code=to_code).first()
    usable = [s for s in (a, b) if s and s.lat is not None and s.lon is not None]
    if not usable:
        return None
    lat = sum(s.lat for s in usable) / len(usable)
    lon = sum(s.lon for s in usable) / len(usable)
    return {
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "from_station_code": from_code,
        "to_station_code": to_code,
        "station_code": (usable[0].station_code),
        "basis": "midpoint of both endpoint stations" if len(usable) == 2 else f"single endpoint {usable[0].station_code}",
    }


# Backwards-compatible alias: earlier revisions resolved weather against the
# corridor's ORIGIN station only. Kept so nothing that imported the old name
# breaks, but corridor_location (midpoint) is what ingestion now uses.
def nearest_station_for_corridor(db: Session, corridor_id: str) -> Optional[models.Station]:
    loc = corridor_location(db, corridor_id)
    if not loc:
        return None
    return db.query(models.Station).filter_by(station_code=loc["station_code"]).first()


def is_forecast_fresh(db: Session, corridor_id: str, ttl_hours: int = FORECAST_TTL_HOURS) -> Optional[dt.datetime]:
    """Returns the last fetch time if this corridor already has a forecast
    younger than the TTL (default 6h), else None. Open-Meteo asks callers
    not to hammer it; re-fetching an unchanged 7-day forecast on every
    scheduler run would be both rude and pointless."""
    row = (
        db.query(models.WeatherHourlyForecast)
        .filter_by(corridor_id=corridor_id)
        .order_by(models.WeatherHourlyForecast.fetched_at.desc())
        .first()
    )
    if not row or not row.fetched_at:
        return None
    age = utc_now() - row.fetched_at
    return row.fetched_at if age <= dt.timedelta(hours=ttl_hours) else None


def _rollup_daily(db: Session, corridor_id: str, station_code: str, hourly_rows: List[dict]) -> int:
    """Builds the DAILY WeatherForecastEntry roll-up from the hourly rows,
    per IST calendar day: worst-case precipitation/wind, minimum visibility,
    lightning if ANY hour that day carries it. This keeps every existing
    daily-granularity consumer (admin rule table, timeline overlay, priority
    scoring) working unchanged on top of the new hourly data."""
    by_day: Dict[dt.date, List[dict]] = {}
    for r in hourly_rows:
        by_day.setdefault(to_ist(r["valid_time"]).date(), []).append(r)

    def _vals(rows, key):
        return [r[key] for r in rows if r.get(key) is not None]

    written = 0
    for ist_day, rows in by_day.items():
        storage_date = _ist_midnight_to_utc(ist_day)
        db.query(models.WeatherForecastEntry).filter(
            models.WeatherForecastEntry.corridor_id == corridor_id,
            models.WeatherForecastEntry.forecast_date == storage_date,
        ).delete()
        temps = _vals(rows, "temperature_c")
        precip = _vals(rows, "precipitation_probability_pct")
        wind = _vals(rows, "wind_speed_kmh")
        vis = _vals(rows, "visibility_km")
        db.add(
            models.WeatherForecastEntry(
                corridor_id=corridor_id,
                station_code=station_code,
                forecast_date=storage_date,
                precipitation_probability_pct=max(precip) if precip else 0.0,
                wind_speed_kmh=max(wind) if wind else 0.0,
                visibility_km=min(vis) if vis else 10.0,
                fog_risk=any(r.get("fog_risk") for r in rows),
                lightning_risk=any(r.get("lightning_risk") for r in rows),
                temperature_max_c=max(temps) if temps else 0.0,
                temperature_min_c=min(temps) if temps else 0.0,
                source="real",
                provider="open_meteo",
            )
        )
        written += 1
    return written


def ingest_forecast_for_corridor(db: Session, corridor_id: str, days: int = 7, user_id: str = "system", force: bool = False) -> dict:
    """Fetches REAL hourly weather from Open-Meteo for the corridor's real
    midpoint coordinates, stores the hourly rows plus a daily roll-up, and
    reports exactly what happened.

    There is NO fabricated fallback. If the provider is unreachable this
    returns available=False with the real error and the URL attempted;
    already-cached rows (if any) stay in place and are reported as stale
    rather than being replaced by invented values."""
    loc = corridor_location(db, corridor_id)
    if not loc:
        raise ValueError(
            f"no real station coordinates available for corridor '{corridor_id}' — load the station master "
            "first (POST /api/data/load-stations), or neither endpoint station has coordinates in the dataset"
        )

    cached_at = is_forecast_fresh(db, corridor_id)
    if cached_at and not force:
        hours = db.query(models.WeatherHourlyForecast).filter_by(corridor_id=corridor_id).count()
        return {
            "corridor_id": corridor_id, "available": True, "used_cache": True,
            "fetched_at_utc": utc_iso(cached_at), "fetched_at_ist": ist_iso(cached_at),
            "hours_cached": hours, "provider": "open_meteo",
            "location": loc, "ttl_hours": FORECAST_TTL_HOURS,
            "note": f"cached forecast is younger than the {FORECAST_TTL_HOURS}h TTL — pass force=true to re-fetch",
        }

    try:
        rows = WEATHER_ADAPTERS["open_meteo"].fetch_forecast(loc["lat"], loc["lon"], days=days)
    except WeatherProviderUnavailable as e:
        existing_hours = db.query(models.WeatherHourlyForecast).filter_by(corridor_id=corridor_id).count()
        log(db, "weather_provider_unavailable", user_id,
            {"corridor_id": corridor_id, "error": str(e), "cached_hours_retained": existing_hours})
        return {
            "corridor_id": corridor_id, "available": False, "used_cache": False,
            "error": str(e), "provider": "open_meteo", "location": loc,
            "cached_hours_retained": existing_hours,
            "note": "weather is UNAVAILABLE — no values were fabricated; any previously cached rows were left untouched",
        }

    for r in rows:
        _validate_hourly_row(r)

    db.query(models.WeatherHourlyForecast).filter_by(corridor_id=corridor_id).delete()
    fetched = utc_now()
    for r in rows:
        db.add(
            models.WeatherHourlyForecast(
                corridor_id=corridor_id, valid_time=r["valid_time"],
                temperature_c=r["temperature_c"], precipitation_probability_pct=r["precipitation_probability_pct"],
                rain_mm=r["rain_mm"], wind_speed_kmh=r["wind_speed_kmh"], visibility_km=r["visibility_km"],
                weather_code=r["weather_code"], lightning_risk=r["lightning_risk"], fog_risk=r["fog_risk"],
                source=r["source"], latitude=r["latitude"], longitude=r["longitude"], fetched_at=fetched,
            )
        )
    days_written = _rollup_daily(db, corridor_id, loc["station_code"], rows)
    db.commit()

    sample = rows[0] if rows else {}
    log(db, "weather_forecast_ingested", user_id,
        {"corridor_id": corridor_id, "hours": len(rows), "days_rolled_up": days_written,
         "lat": loc["lat"], "lon": loc["lon"], "provider": "open_meteo"})

    return {
        "corridor_id": corridor_id, "available": True, "used_cache": False,
        "provider": "open_meteo", "location": loc,
        "hours_ingested": len(rows), "days_rolled_up": days_written,
        "fetched_at_utc": utc_iso(fetched), "fetched_at_ist": ist_iso(fetched),
        "first_hour_ist": ist_iso(sample.get("valid_time")) if sample else None,
        "sample_hour": {
            "valid_ist": ist_iso(sample.get("valid_time")) if sample else None,
            "temperature_c": sample.get("temperature_c"),
            "precipitation_probability_pct": sample.get("precipitation_probability_pct"),
            "wind_speed_kmh": sample.get("wind_speed_kmh"),
            "visibility_km": sample.get("visibility_km"),
            "weather_code": sample.get("weather_code"),
        } if sample else None,
    }


def hourly_rows_for_window(db: Session, corridor_id: str, start: dt.datetime, end: dt.datetime) -> List[models.WeatherHourlyForecast]:
    """Every hourly forecast row overlapping [start, end) — the precise
    input the optimizer's per-window weather check uses, instead of a
    whole-day aggregate."""
    return (
        db.query(models.WeatherHourlyForecast)
        .filter(
            models.WeatherHourlyForecast.corridor_id == corridor_id,
            models.WeatherHourlyForecast.valid_time < end,
            models.WeatherHourlyForecast.valid_time >= start - dt.timedelta(hours=1),
        )
        .order_by(models.WeatherHourlyForecast.valid_time)
        .all()
    )


def get_forecast_rows(db: Session, corridor_ids: List[str], date_from: dt.date, date_to: dt.date) -> Dict[Tuple[str, dt.date], models.WeatherForecastEntry]:
    """Batch-fetches every forecast row for the given corridors/date range in
    one query and indexes it by (corridor_id, date) — same caching shape as
    scheduler.get_safe_slots' per-corridor occurrence cache, for the same
    reason: this is called once per (task, candidate-window) pair during
    scheduling and must not turn into an N+1 query storm."""
    if not corridor_ids:
        return {}
    # date_from/date_to are IST calendar dates; forecast_date is stored as
    # the UTC-equivalent of IST midnight (see _ist_midnight_to_utc), so the
    # query bounds must go through the same conversion — comparing against
    # naive local midnight here would silently miss/include rows near the
    # IST/UTC offset boundary.
    lower = _ist_midnight_to_utc(date_from)
    upper = _ist_midnight_to_utc(date_to + dt.timedelta(days=1))
    rows = (
        db.query(models.WeatherForecastEntry)
        .filter(
            models.WeatherForecastEntry.corridor_id.in_(sorted(set(corridor_ids))),
            models.WeatherForecastEntry.forecast_date >= lower,
            models.WeatherForecastEntry.forecast_date < upper,
        )
        .all()
    )
    return {(r.corridor_id, forecast_date_ist(r)): r for r in rows}


# ============================================================ Layer 3: priority scoring

def weather_priority_bonus(db: Session, task: models.MaintenanceTask, w_weather_risk: float, days_ahead: int = RELIABLE_HORIZON_DAYS) -> Tuple[float, str]:
    """Checks the task's own corridor for a forecast, over the next
    `days_ahead` days, that trips a 'priority' rule matching this task's
    defect_type. Returns (points, justification_fragment) — fragment is ""
    when nothing applies, so callers can append unconditionally. Never
    raises: a missing/unreachable forecast simply contributes 0 points,
    exactly like an unregistered asset defaults to neutral criticality."""
    try:
        rules = [r for r in get_rules(db, mode="priority") if _matches(r, task.defect_type)]
        if not rules:
            return 0.0, ""

        today = ist_today()
        forecast_by_date = get_forecast_rows(db, [task.corridor_id], today, today + dt.timedelta(days=days_ahead))
        if not forecast_by_date:
            return 0.0, ""

        best = None  # (points, rule, day_offset, forecast_row)
        for (corridor_id, date), fc in forecast_by_date.items():
            for rule in rules:
                triggered, _detail = _hazard_triggered(rule, fc)
                if triggered:
                    points = round(w_weather_risk * rule.priority_points, 1)
                    if best is None or points > best[0]:
                        best = (points, rule, date, fc)

        if not best:
            return 0.0, ""
        points, rule, date, fc = best
        days_out = (date - today).days
        when = "today" if days_out == 0 else f"in the next {max(days_out, 1)} day(s)"
        hazard_desc = {
            "rain": f"forecasted heavy rain ({fc.precipitation_probability_pct:.0f}% probability)",
            "lightning": "forecasted lightning risk",
            "wind": f"forecasted high wind ({fc.wind_speed_kmh:.0f} km/h)",
            "heat": f"forecasted extreme heat ({fc.temperature_max_c:.0f}°C)",
            "fog": f"forecasted fog (visibility {fc.visibility_km:.1f} km)",
        }.get(rule.hazard, f"forecasted {rule.hazard}")
        fragment = (
            f" {hazard_desc.capitalize()} {when} on {task.corridor_id} added {points:.1f} pts "
            f"to {rule.label.lower() if rule.label else rule.hazard + '-related risk'}."
        )
        return points, fragment
    except Exception:
        return 0.0, ""


# ============================================================ Layer 4: optimizer feasibility

def _hazard_triggered(rule: models.WeatherSensitivityRule, fc: models.WeatherForecastEntry) -> Tuple[bool, str]:
    if rule.hazard == "lightning":
        return bool(fc.lightning_risk), "lightning risk forecast"
    if rule.hazard == "wind":
        return fc.wind_speed_kmh > rule.threshold, f"wind {fc.wind_speed_kmh:.0f} km/h forecast (threshold {rule.threshold:.0f})"
    if rule.hazard == "heat":
        return fc.temperature_max_c > rule.threshold, f"temperature {fc.temperature_max_c:.0f}°C forecast (threshold {rule.threshold:.0f})"
    if rule.hazard == "rain":
        return fc.precipitation_probability_pct > rule.threshold, f"rain probability {fc.precipitation_probability_pct:.0f}% forecast (threshold {rule.threshold:.0f})"
    if rule.hazard == "fog":
        return fc.visibility_km < rule.threshold, f"visibility {fc.visibility_km:.1f} km forecast (below {rule.threshold:.1f} km threshold)"
    return False, ""


class _WindowForecast:
    """Worst-case conditions across exactly the hours a candidate window
    spans. Exposes the same attribute names as a daily WeatherForecastEntry
    so _hazard_triggered() — and therefore every admin-configured rule —
    works against it unchanged, whether the underlying data is hourly or
    daily."""

    __slots__ = ("precipitation_probability_pct", "wind_speed_kmh", "visibility_km",
                 "temperature_max_c", "temperature_min_c", "lightning_risk", "fog_risk", "hours")

    def __init__(self, rows):
        def vals(attr):
            return [getattr(r, attr) for r in rows if getattr(r, attr) is not None]

        precip, wind, vis = vals("precipitation_probability_pct"), vals("wind_speed_kmh"), vals("visibility_km")
        temps = vals("temperature_c")
        self.precipitation_probability_pct = max(precip) if precip else 0.0
        self.wind_speed_kmh = max(wind) if wind else 0.0
        self.visibility_km = min(vis) if vis else 10.0
        self.temperature_max_c = max(temps) if temps else 0.0
        self.temperature_min_c = min(temps) if temps else 0.0
        self.lightning_risk = any(r.lightning_risk for r in rows)
        self.fog_risk = any(r.fog_risk for r in rows)
        self.hours = len(rows)


def _window_forecast(corridor_hourly, window_start: dt.datetime, window_end: dt.datetime):
    """Aggregates the hourly rows overlapping [window_start, window_end).
    Returns None when there is no hourly coverage, so the caller can fall
    back to the daily roll-up rather than silently assuming fair weather."""
    if not corridor_hourly:
        return None
    rows = [
        r for r in corridor_hourly
        if r.valid_time < window_end and (r.valid_time + dt.timedelta(hours=1)) > window_start
    ]
    return _WindowForecast(rows) if rows else None


def assess_candidates(
    db: Session,
    tasks: List[models.MaintenanceTask],
    corridor_matched_slots: Dict[str, list],
    horizon_start: dt.date,
    horizon_days: int,
) -> dict:
    """The single entry point scheduler.py calls before duration-eligibility
    filtering. corridor_matched_slots is {task_id: [CorridorSlot, ...]}
    already filtered to matching corridor (duration NOT yet checked).

    Returns:
      eligible: {task_id: [CorridorSlot, ...]} — weather-hard-safe candidates
          whose slot is long enough for the WEATHER-ADJUSTED duration.
      effective_duration: {(task_id, slot_id): dict(base_hours, effective_hours,
          buffer_pct, note)}
      weather_exclusion_reason: {task_id: str} — set only for a task that
          ends up with ZERO eligible candidates AND had at least one
          candidate removed specifically by a hard weather rule, so
          scheduler._build_reason can report the weather-specific reason
          instead of the generic "corridor unavailability" one.
    """
    hard_rules_all = get_rules(db, mode="hard")
    soft_rules_all = get_rules(db, mode="soft")
    if not hard_rules_all and not soft_rules_all:
        # nothing configured — behave exactly as before weather was added
        eligible = {tid: list(slots) for tid, slots in corridor_matched_slots.items()}
        return {"eligible": eligible, "effective_duration": {}, "weather_exclusion_reason": {}}

    all_corridors = {t.corridor_id for t in tasks}
    horizon_end = horizon_start + dt.timedelta(days=horizon_days)
    forecast_by_date = get_forecast_rows(db, list(all_corridors), horizon_start, horizon_end)
    today = ist_today()

    # HOURLY forecast rows, batched once per corridor. The per-window check
    # below prefers these over the daily roll-up: a window is a few hours,
    # and "thunderstorm somewhere today" must not exclude an otherwise-clear
    # 02:00-05:00 window. Falls back to the daily row when no hourly data
    # has been ingested for that corridor.
    hourly_by_corridor: Dict[str, list] = {}
    if all_corridors:
        h_start, _ = ist_date_to_utc_bounds(horizon_start)
        h_end = h_start + dt.timedelta(days=horizon_days + 1)
        for row in (
            db.query(models.WeatherHourlyForecast)
            .filter(
                models.WeatherHourlyForecast.corridor_id.in_(sorted(all_corridors)),
                models.WeatherHourlyForecast.valid_time >= h_start,
                models.WeatherHourlyForecast.valid_time < h_end,
            )
            .order_by(models.WeatherHourlyForecast.valid_time)
            .all()
        ):
            hourly_by_corridor.setdefault(row.corridor_id, []).append(row)

    tasks_by_id = {t.task_id: t for t in tasks}
    eligible: Dict[str, list] = {}
    effective_duration: Dict[tuple, dict] = {}
    weather_exclusion_reason: Dict[str, str] = {}

    for task_id, slots in corridor_matched_slots.items():
        task = tasks_by_id[task_id]
        hard_rules = [r for r in hard_rules_all if _matches(r, task.defect_type)]
        soft_rules = [r for r in soft_rules_all if _matches(r, task.defect_type)]

        kept = []
        hard_exclusions_seen = []
        for s in slots:
            # forecast_by_date is keyed by IST calendar date (see
            # get_forecast_rows/forecast_date_ist) — s.start_time is
            # naive-but-UTC, so its IST calendar date must go through
            # to_ist() first, not a bare .date() (which would silently use
            # the UTC day near the IST/UTC offset boundary).
            slot_date = to_ist(s.start_time).date()
            # Prefer an hour-accurate forecast for exactly this window;
            # fall back to the day's roll-up only when no hourly data exists.
            fc = _window_forecast(hourly_by_corridor.get(s.corridor_id), s.start_time, s.end_time)
            granularity = "hourly"
            if fc is None:
                fc = forecast_by_date.get((s.corridor_id, slot_date))
                granularity = "daily"

            hard_hit = None
            if fc is not None and hard_rules and (slot_date - today).days <= RELIABLE_HORIZON_DAYS:
                for rule in hard_rules:
                    triggered, detail = _hazard_triggered(rule, fc)
                    if triggered:
                        hard_hit = (rule, detail)
                        break
            if hard_hit:
                rule, detail = hard_hit
                window_label = f"{to_ist(s.start_time).strftime('%Y-%m-%d %H:%M')}-{to_ist(s.end_time).strftime('%H:%M')} IST"
                hard_exclusions_seen.append(
                    f"{rule.hazard} risk forecast for the {window_label} window on {s.corridor_id} "
                    f"({detail}, {granularity} forecast)"
                )
                continue  # excluded — never offered to the solver, not merely deprioritized

            base_hours = task.required_duration_hours
            eff_hours = base_hours
            buffer_pct = 0.0
            note = ""
            if fc is not None and soft_rules:
                for rule in soft_rules:
                    triggered, detail = _hazard_triggered(rule, fc)
                    if triggered and rule.duration_buffer_pct > buffer_pct:
                        buffer_pct = rule.duration_buffer_pct
                        note = f"{rule.label or rule.hazard} ({detail}): +{rule.duration_buffer_pct:.0f}% duration buffer"
                if buffer_pct:
                    eff_hours = round(base_hours * (1 + buffer_pct / 100.0), 2)

            effective_duration[(task_id, s.slot_id)] = {
                "base_hours": base_hours, "effective_hours": eff_hours, "buffer_pct": buffer_pct, "note": note,
            }
            if _slot_duration_hours(s) >= eff_hours:
                kept.append(s)

        eligible[task_id] = kept
        if not kept and hard_exclusions_seen:
            weather_exclusion_reason[task_id] = (
                "No weather-safe corridor slot found: " + "; ".join(sorted(set(hard_exclusions_seen))[:3]) + "."
            )

    return {"eligible": eligible, "effective_duration": effective_duration, "weather_exclusion_reason": weather_exclusion_reason}


def _slot_duration_hours(slot) -> float:
    return (slot.end_time - slot.start_time).total_seconds() / 3600.0
