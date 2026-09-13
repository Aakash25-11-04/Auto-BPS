"""Weather forecast endpoints (Layer 1/2 surface).

RBAC: ingesting a forecast is a Control Office operational action, same tier
as loading the timetable or deriving corridor availability — COA-only.
Reading the forecast (for the timeline overlay) and the adapter health check
carry no department-scoped task data, so any authenticated user may view
them — matching GET /api/corridor-availability and GET /api/stations.
"""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import models
import schemas
import weather_service
from adapters.weather import WEATHER_ADAPTERS
from database import get_db
from tz_utils import ist_date_to_utc_bounds, ist_iso, ist_today

router = APIRouter(prefix="/api/weather", tags=["weather"])

HORIZON_DAYS = {"weekly": 7, "monthly": 30}


@router.get("/forecast")
def get_forecast(
    corridor_id: str,
    horizon: str = "weekly",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Returns exactly the WeatherForecastEntry rows the optimizer itself
    reads (see weather_service.get_forecast_rows) — the frontend's timeline
    overlay renders this same data, so what's shown can never drift from
    what's enforced."""
    if horizon not in HORIZON_DAYS:
        raise HTTPException(status_code=400, detail="horizon must be 'weekly' or 'monthly'")
    today = ist_today()
    # forecast_date is stored as the UTC-equivalent of IST midnight (see
    # weather_service._ist_midnight_to_utc) — bounds must go through the
    # same conversion, not naive local midnight.
    lower, _ = ist_date_to_utc_bounds(today)
    upper, _ = ist_date_to_utc_bounds(today + dt.timedelta(days=HORIZON_DAYS[horizon]))
    rows = (
        db.query(models.WeatherForecastEntry)
        .filter(
            models.WeatherForecastEntry.corridor_id == corridor_id,
            models.WeatherForecastEntry.forecast_date >= lower,
            models.WeatherForecastEntry.forecast_date <= upper,
        )
        .order_by(models.WeatherForecastEntry.forecast_date)
        .all()
    )
    return [
        {
            "corridor_id": r.corridor_id,
            "station_code": r.station_code,
            "forecast_date": r.forecast_date,
            "forecast_date_ist": weather_service.forecast_date_ist(r).isoformat(),
            "precipitation_probability_pct": r.precipitation_probability_pct,
            "wind_speed_kmh": r.wind_speed_kmh,
            "visibility_km": r.visibility_km,
            "fog_risk": r.fog_risk,
            "lightning_risk": r.lightning_risk,
            "temperature_max_c": r.temperature_max_c,
            "temperature_min_c": r.temperature_min_c,
            "source": r.source,
            "provider": r.provider,
            "fetched_at": r.fetched_at,
            "reliable_forecast": (weather_service.forecast_date_ist(r) - today).days <= weather_service.RELIABLE_HORIZON_DAYS,
        }
        for r in rows
    ]


@router.post("/ingest")
def ingest_forecast(
    payload: schemas.WeatherIngestRequest,
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    """Fetches REAL hourly weather from Open-Meteo for the corridor's real
    midpoint coordinates. Cached for FORECAST_TTL_HOURS (pass force=true to
    bypass). If the provider is unreachable the response carries
    available=false with the real error — no values are ever fabricated."""
    try:
        return weather_service.ingest_forecast_for_corridor(
            db, payload.corridor_id, payload.days, current_user.user_id, force=force
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/hourly")
def get_hourly(
    corridor_id: str,
    hours: int = 48,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """The hourly rows the optimizer's per-window check actually reads.
    Returns availability status explicitly: when nothing has been
    ingested (or the provider was unreachable), this says so rather than
    returning an empty list that could be mistaken for fair weather."""
    rows = (
        db.query(models.WeatherHourlyForecast)
        .filter(models.WeatherHourlyForecast.corridor_id == corridor_id)
        .order_by(models.WeatherHourlyForecast.valid_time)
        .limit(max(1, min(hours, 24 * 16)))
        .all()
    )
    if not rows:
        return {
            "corridor_id": corridor_id, "available": False, "hours": [],
            "reason": "no weather has been ingested for this corridor yet (or the provider was unreachable) — "
                      "weather is UNAVAILABLE for it, not assumed clear",
        }
    return {
        "corridor_id": corridor_id,
        "available": True,
        "source": rows[0].source,
        "latitude": rows[0].latitude,
        "longitude": rows[0].longitude,
        "fetched_at": rows[0].fetched_at,
        "hours": [
            {
                "valid_time": r.valid_time,
                "valid_ist": ist_iso(r.valid_time),
                "temperature_c": r.temperature_c,
                "precipitation_probability_pct": r.precipitation_probability_pct,
                "rain_mm": r.rain_mm,
                "wind_speed_kmh": r.wind_speed_kmh,
                "visibility_km": r.visibility_km,
                "weather_code": r.weather_code,
                "lightning_risk": r.lightning_risk,
                "fog_risk": r.fog_risk,
            }
            for r in rows
        ],
    }


@router.get("/health")
def weather_health(current_user: models.User = Depends(auth.get_current_user)):
    return {name: adapter.health_check() for name, adapter in WEATHER_ADAPTERS.items()}


@router.get("/assessments")
def get_assessments(
    plan_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Audit view: every weather-influenced outcome (soft duration buffer
    applied, or hard-excluded) recorded for a given plan — base vs.
    weather-adjusted duration made explicit, per the spec's auditability
    requirement."""
    rows = db.query(models.TaskWeatherAssessment).filter_by(plan_id=plan_id).all()
    return [
        {
            "task_id": r.task_id, "corridor_id": r.corridor_id, "slot_id": r.slot_id, "outcome": r.outcome,
            "base_duration_hours": r.base_duration_hours, "effective_duration_hours": r.effective_duration_hours,
            "buffer_pct_applied": r.buffer_pct_applied, "hazard": r.hazard, "reason": r.reason,
        }
        for r in rows
    ]
