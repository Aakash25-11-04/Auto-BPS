"""Corridor-range search (read-only) — §10.

Any user picks a FROM/TO station and gets every real train moving through
that corridor, the weather forecast for it, and existing corridor
availability/block-plan context — all for one IST calendar date. This never
touches scheduling, the optimizer, or governance: it's pure reads plus one
optional, cheap audit-log entry (matching how every other read-mostly action
in this system is still logged, e.g. plan_exported).

Station resolution supports fuzzy matching (substring on name, case-
insensitive, or exact code) since a user won't necessarily know a station's
short code. Corridor resolution is EXACT when a direct single-hop segment
exists between the two stations in the loaded real timetable data (this
dataset's corridor_id is, by construction, an adjacent-stop pair — see
timetable_loader.py) in either direction. When no direct segment exists,
this returns the busiest real corridor touching the FROM station as an
APPROXIMATE suggestion, explicitly flagged — never silently presented as
if it were the requested corridor.
"""
import datetime as dt

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
import station_loader
import timetable_loader
import weather_service
from tz_utils import ist_iso


def _resolve_station(db: Session, query: str):
    """Exact code match wins; then a well-known alternate code (e.g. MMCT ->
    BCT for Mumbai Central, see station_loader.STATION_CODE_ALIASES); then
    the first case-insensitive substring match on station_name. Returns None
    if nothing matches — never a fabricated station."""
    if not query:
        return None
    q = query.strip()
    exact = db.query(models.Station).filter(func.upper(models.Station.station_code) == q.upper()).first()
    if exact:
        return exact
    aliased = station_loader.resolve_alias(q)
    if aliased != q.upper():
        by_alias = db.query(models.Station).filter(func.upper(models.Station.station_code) == aliased).first()
        if by_alias:
            return by_alias
    return (
        db.query(models.Station)
        .filter(models.Station.station_name.ilike(f"%{q}%"))
        .order_by(func.length(models.Station.station_name))
        .first()
    )


def _station_out(s: models.Station) -> dict:
    return {"station_code": s.station_code, "station_name": s.station_name, "zone": s.zone, "lat": s.lat, "lon": s.lon}


def _corridor_has_data(db: Session, corridor_id: str) -> bool:
    return db.query(models.TrainTimetableEntry.id).filter_by(corridor_id=corridor_id).first() is not None


def _busiest_corridor_touching(db: Session, station_code: str):
    """Best-effort approximate match: the real corridor with the most
    segments that starts OR ends at station_code. Returns (corridor_id,
    segment_count) or (None, 0) if nothing touches this station at all."""
    rows = (
        db.query(models.TrainTimetableEntry.corridor_id, func.count().label("n"))
        .filter(
            (models.TrainTimetableEntry.from_station_code == station_code)
            | (models.TrainTimetableEntry.to_station_code == station_code)
        )
        .group_by(models.TrainTimetableEntry.corridor_id)
        .order_by(func.count().desc())
        .first()
    )
    return (rows[0], rows[1]) if rows else (None, 0)


def search(db: Session, from_query: str, to_query: str, ist_date: dt.date, days_ahead_weather: int = 6) -> dict:
    from_station = _resolve_station(db, from_query)
    to_station = _resolve_station(db, to_query)
    if not from_station:
        raise ValueError(f"no station matches '{from_query}' — try a station code or a substring of its name")
    if not to_station:
        raise ValueError(f"no station matches '{to_query}' — try a station code or a substring of its name")
    if from_station.station_code == to_station.station_code:
        raise ValueError(f"from_station and to_station both resolved to the same station ({from_station.station_code})")

    forward_id = f"{from_station.station_code}-{to_station.station_code}"
    reverse_id = f"{to_station.station_code}-{from_station.station_code}"
    matched_ids = [cid for cid in (forward_id, reverse_id) if _corridor_has_data(db, cid)]

    corridor_match = "exact"
    match_note = None
    if not matched_ids:
        approx_id, segment_count = _busiest_corridor_touching(db, from_station.station_code)
        if not approx_id:
            approx_id, segment_count = _busiest_corridor_touching(db, to_station.station_code)
        if not approx_id:
            raise ValueError(
                f"no corridor connects {from_station.station_code} and {to_station.station_code}, and no loaded "
                f"corridor touches either station — load the real timetable first (POST /api/data/load-timetable)"
            )
        matched_ids = [approx_id]
        corridor_match = "approximate"
        match_note = (
            f"No direct real segment exists between {from_station.station_code} and {to_station.station_code} in "
            f"the loaded timetable data. Showing '{approx_id}' instead — the busiest real corridor "
            f"({segment_count} segments/week) touching one of your requested stations."
        )

    trains = []
    for cid in matched_ids:
        direction = "forward" if cid == forward_id else ("reverse" if cid == reverse_id else "nearest")
        for row in timetable_loader.train_occurrences_on_date(db, cid, ist_date):
            trains.append(
                {
                    "train_number": row["train_id"],
                    "train_name": row["train_name"],
                    "direction": direction,
                    "from_station_code": row["from_station_code"],
                    "to_station_code": row["to_station_code"],
                    "departure_ist": ist_iso(row["departure"]),
                    "arrival_ist": ist_iso(row["arrival"]),
                    "service_type": row["service_type"] or None,
                }
            )
    trains.sort(key=lambda r: r["departure_ist"])

    primary_corridor_id = matched_ids[0]
    weather_rows = (
        db.query(models.WeatherForecastEntry)
        .filter(
            models.WeatherForecastEntry.corridor_id.in_(matched_ids),
            models.WeatherForecastEntry.forecast_date >= dt.datetime.combine(ist_date, dt.time.min),
            models.WeatherForecastEntry.forecast_date <= dt.datetime.combine(ist_date + dt.timedelta(days=days_ahead_weather), dt.time.min),
        )
        .order_by(models.WeatherForecastEntry.forecast_date)
        .all()
    )
    # Both directions of a corridor (SBB-GZB and GZB-SBB) can each carry a
    # forecast for the same day, which would render the same date twice.
    # Keep one row per IST date, preferring the primary corridor's own.
    seen_dates = {}
    deduped = []
    for w in weather_rows:
        key = weather_service.forecast_date_ist(w)
        if key in seen_dates:
            if w.corridor_id == primary_corridor_id and seen_dates[key].corridor_id != primary_corridor_id:
                deduped[deduped.index(seen_dates[key])] = w
                seen_dates[key] = w
            continue
        seen_dates[key] = w
        deduped.append(w)
    weather_rows = sorted(deduped, key=weather_service.forecast_date_ist)

    weather = [
        {
            "date_ist": weather_service.forecast_date_ist(w).isoformat(),
            "precipitation_probability_pct": w.precipitation_probability_pct,
            "wind_speed_kmh": w.wind_speed_kmh,
            "visibility_index": w.visibility_km,
            "lightning_risk": w.lightning_risk,
            "fog_risk": w.fog_risk,
            "temperature_max_c": w.temperature_max_c,
            "temperature_min_c": w.temperature_min_c,
            "source": w.source,
            "provider": w.provider,
            "valid_from_ist": ist_iso(dt.datetime.combine(weather_service.forecast_date_ist(w), dt.time.min)),
            "valid_to_ist": ist_iso(dt.datetime.combine(weather_service.forecast_date_ist(w) + dt.timedelta(days=1), dt.time.min)),
        }
        for w in weather_rows
    ]

    slots = (
        db.query(models.CorridorSlot)
        .filter(models.CorridorSlot.corridor_id.in_(matched_ids))
        .order_by(models.CorridorSlot.start_time)
        .all()
    )
    corridor_availability = [
        {
            "slot_id": s.slot_id,
            "corridor_id": s.corridor_id,
            "start_ist": ist_iso(s.start_time),
            "end_ist": ist_iso(s.end_time),
            "status": s.status,
            "derived_from": s.derived_from,
            "horizon": s.horizon,
        }
        for s in slots
    ]

    plans = (
        db.query(models.BlockPlan)
        .filter(models.BlockPlan.status.in_(["draft", "published"]))
        .all()
    )
    plan_status_by_id = {p.plan_id: p.status for p in plans}
    entries = (
        db.query(models.BlockPlanEntry)
        .filter(
            models.BlockPlanEntry.corridor_id.in_(matched_ids),
            models.BlockPlanEntry.plan_id.in_(list(plan_status_by_id.keys()) or ["__none__"]),
        )
        .order_by(models.BlockPlanEntry.assigned_window_start)
        .all()
    )
    existing_blocks = [
        {
            "plan_id": e.plan_id,
            "plan_status": plan_status_by_id.get(e.plan_id, "unknown"),
            "task_id": e.task_id,
            "department": e.department,
            "co_scheduled_departments": [d for d in (e.co_scheduled_departments or "").split(",") if d],
            "start_ist": ist_iso(e.assigned_window_start),
            "end_ist": ist_iso(e.assigned_window_end),
        }
        for e in entries
    ]

    return {
        "corridor_id": primary_corridor_id,
        "matched_corridor_ids": matched_ids,
        "corridor_match": corridor_match,
        "match_note": match_note,
        "from_station": _station_out(from_station),
        "to_station": _station_out(to_station),
        "date_ist": ist_date.isoformat(),
        "trains": trains,
        "weather": weather,
        "corridor_availability": corridor_availability,
        "existing_blocks": existing_blocks,
    }
