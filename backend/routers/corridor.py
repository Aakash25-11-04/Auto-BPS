"""Real timetable ingestion, corridor availability (manual + derived from
genuine traffic gaps), stations, and freight forecast endpoints.

RBAC: loading the real timetable, deriving/defining corridor availability,
and logging freight forecasts are Control Office operational actions —
restricted to COA (require_role("COA")), matching the explicit "COA only"
rule given for POST /api/corridor-availability, extended here for
consistency to the closely-related derive/load-timetable/freight-forecast
writes. Reads (stations, timetable, corridor availability, freight
forecast) carry no department-scoped task data, so any authenticated user
may view them.
"""
import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import corridor_builder
import corridor_search
import models
import schemas
import station_loader
import timetable_loader
import vacancy
from audit import log
from database import get_db
from tz_utils import IST, UTC, ist_date_to_utc_bounds, ist_iso, ist_today

router = APIRouter(tags=["corridor"])

HORIZON_DAYS = {"weekly": 7, "monthly": 30}


@router.post("/api/data/load-timetable")
def load_timetable(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_role("COA"))):
    if not timetable_loader.files_present():
        raise HTTPException(
            status_code=503,
            detail=(
                "Real timetable source files are missing under data/raw/. Fetch them from "
                f"{timetable_loader.SOURCE_URL} (and the matching stations.json) before loading — "
                "no synthetic data will be substituted."
            ),
        )
    station_report = station_loader.load_stations(db, include_remote=True)
    result = timetable_loader.load_timetable(db)
    result["station_master"] = station_report
    log(db, "timetable_loaded", current_user.user_id,
        {k: v for k, v in result.items() if k != "station_master"})
    return result


@router.post("/api/data/load-stations")
def load_station_master(
    include_remote: bool = True,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    """Fix 1: (re)loads and MERGES the real station master from the open
    datasets, idempotently (upsert — safe to re-run). Reports which sources
    were used, which failed (with the URL and error, never silently), and
    which timetable station codes the master is still missing."""
    report = station_loader.load_stations(db, include_remote=include_remote)
    log(db, "station_master_loaded", current_user.user_id,
        {"total": report["total_stations"], "with_coordinates": report["with_coordinates"],
         "created": report["stations_created"], "updated": report["stations_updated"]})
    return report


@router.post("/api/corridors/build")
def build_corridors(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_role("COA"))):
    """Fix 2: derives the REAL corridor master from the loaded timetable —
    every physical section, plus junction-to-junction route corridors with
    their ordered intermediate stations, real polyline geometry and
    distance. Re-runnable (upsert by corridor_id)."""
    try:
        report = corridor_builder.build_corridors(db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log(db, "corridors_built", current_user.user_id, report)
    return report


@router.get("/api/corridors")
def list_corridors(
    kind: str = None,
    q: str = None,
    min_trains: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    query = db.query(models.Corridor)
    if kind:
        query = query.filter(models.Corridor.kind == kind)
    if q:
        query = query.filter(models.Corridor.corridor_id.like(f"%{q.upper()}%"))
    if min_trains:
        query = query.filter(models.Corridor.train_count >= min_trains)
    rows = query.order_by(models.Corridor.train_count.desc()).limit(limit).all()
    return [
        {
            "corridor_id": c.corridor_id, "kind": c.kind,
            "from_station_code": c.from_station_code, "to_station_code": c.to_station_code,
            "intermediate_stations": json.loads(c.intermediate_stations or "[]"),
            "section_ids": json.loads(c.section_ids or "[]"),
            "section_count": c.section_count, "zone": c.zone,
            "total_distance_km": c.total_distance_km, "train_count": c.train_count,
            "derived_from": c.derived_from,
        }
        for c in rows
    ]


@router.get("/api/corridors/{corridor_id}")
def get_corridor(corridor_id: str, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    c = db.query(models.Corridor).filter_by(corridor_id=corridor_id).first()
    if not c:
        raise HTTPException(status_code=404, detail=f"corridor '{corridor_id}' not found — run POST /api/corridors/build after loading the timetable")
    station_codes = [c.from_station_code] + json.loads(c.intermediate_stations or "[]") + [c.to_station_code]
    stations = {s.station_code: s for s in db.query(models.Station).filter(models.Station.station_code.in_(station_codes)).all()}
    return {
        "corridor_id": c.corridor_id, "kind": c.kind,
        "from_station_code": c.from_station_code, "to_station_code": c.to_station_code,
        "intermediate_stations": json.loads(c.intermediate_stations or "[]"),
        "section_ids": json.loads(c.section_ids or "[]"),
        "section_count": c.section_count, "zone": c.zone,
        "total_distance_km": c.total_distance_km, "train_count": c.train_count,
        "derived_from": c.derived_from,
        "geometry": json.loads(c.geometry_json) if c.geometry_json else None,
        "stations": [
            {
                "station_code": code,
                "station_name": stations[code].station_name if code in stations else None,
                "station_type": stations[code].station_type if code in stations else None,
                "state": stations[code].state if code in stations else None,
                "lat": stations[code].lat if code in stations else None,
                "lon": stations[code].lon if code in stations else None,
            }
            for code in station_codes
        ],
    }


@router.get("/api/stations")
def list_stations(db: Session = Depends(get_db), q: str = None, limit: int = 100, current_user: models.User = Depends(auth.get_current_user)):
    query = db.query(models.Station)
    if q:
        like = f"%{q.upper()}%"
        query = query.filter(
            (models.Station.station_code.like(like)) | (models.Station.station_name.ilike(f"%{q}%"))
        )
    rows = query.limit(limit).all()
    return [{"station_code": r.station_code, "station_name": r.station_name, "zone": r.zone, "lat": r.lat, "lon": r.lon} for r in rows]


@router.get("/api/corridor-search")
def search_corridor(
    from_station: str,
    to_station: str,
    date: dt.date = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """§10: read-only corridor-range search — any authenticated user (every
    role, not one department), reachable without touching scheduling, the
    optimizer, or governance. `date` is an IST CALENDAR date: if omitted, it
    defaults to today IN IST (ist_today(), not the server's local/UTC date —
    these can genuinely differ near midnight, exactly the bug this whole
    timezone fix targets)."""
    search_date = date or ist_today()
    try:
        result = corridor_search.search(db, from_station, to_station, search_date)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Cheap, read-only audit trail (explicitly allowed by the feature spec)
    # — no scheduling/task/governance state is touched.
    log(
        db, "corridor_search", current_user.user_id,
        {"from_station": from_station, "to_station": to_station, "date_ist": search_date.isoformat(),
         "corridor_id": result["corridor_id"], "corridor_match": result["corridor_match"]},
    )
    return result


@router.get("/api/network-map")
def network_map(
    limit_corridors: int = 20,
    horizon: str = "weekly",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Layer 6 network map data: the busiest real corridors, their real
    station coordinates (when the loaded dataset has them), and how much
    scheduled block activity each corridor currently carries — everything
    the frontend needs to plot stations + corridors + activity without any
    further backend round-trips."""
    top = timetable_loader.top_corridors(db, limit_corridors)
    corridor_ids = [c["corridor_id"] for c in top]

    endpoints = set()
    for cid in corridor_ids:
        parts = cid.split("-")
        if len(parts) == 2:
            endpoints.update(parts)
    stations = db.query(models.Station).filter(models.Station.station_code.in_(endpoints)).all()
    station_by_code = {s.station_code: {"station_code": s.station_code, "station_name": s.station_name, "lat": s.lat, "lon": s.lon} for s in stations}

    plan = (
        db.query(models.BlockPlan)
        .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
        .order_by(models.BlockPlan.version.desc())
        .first()
    )
    activity_by_corridor = {}
    if plan:
        entries = db.query(models.BlockPlanEntry).filter_by(plan_id=plan.plan_id).all()
        for e in entries:
            activity_by_corridor.setdefault(e.corridor_id, {"blocks": 0, "departments": set()})
            activity_by_corridor[e.corridor_id]["blocks"] += 1
            activity_by_corridor[e.corridor_id]["departments"].add(e.department)

    corridors = []
    for c in top:
        parts = c["corridor_id"].split("-")
        if len(parts) != 2:
            continue
        a, b = parts
        act = activity_by_corridor.get(c["corridor_id"])
        corridors.append(
            {
                "corridor_id": c["corridor_id"],
                "segment_count": c["segment_count"],
                "from_station": a,
                "to_station": b,
                "active_blocks": act["blocks"] if act else 0,
                "active_departments": sorted(act["departments"]) if act else [],
            }
        )

    return {"stations": list(station_by_code.values()), "corridors": corridors}


@router.get("/api/timetable")
def get_timetable(corridor_id: str = None, db: Session = Depends(get_db), limit: int = 200, current_user: models.User = Depends(auth.get_current_user)):
    query = db.query(models.TrainTimetableEntry)
    if corridor_id:
        query = query.filter(models.TrainTimetableEntry.corridor_id == corridor_id)
    rows = query.order_by(models.TrainTimetableEntry.scheduled_departure).limit(limit).all()
    return [
        {
            "train_id": r.train_id,
            "train_name": r.train_name,
            "from_station_code": r.from_station_code,
            "to_station_code": r.to_station_code,
            "corridor_id": r.corridor_id,
            "scheduled_departure": r.scheduled_departure,
            "scheduled_arrival": r.scheduled_arrival,
        }
        for r in rows
    ]


@router.get("/api/timetable/top-corridors")
def top_corridors(db: Session = Depends(get_db), limit: int = 15, current_user: models.User = Depends(auth.get_current_user)):
    return timetable_loader.top_corridors(db, limit)


@router.get("/api/corridor-availability")
def list_corridor_availability(
    corridor_id: str = None,
    horizon: str = "weekly",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    query = db.query(models.CorridorSlot).filter(models.CorridorSlot.horizon == horizon)
    if corridor_id:
        query = query.filter(models.CorridorSlot.corridor_id == corridor_id)
    rows = query.order_by(models.CorridorSlot.start_time).all()
    return [
        {
            "slot_id": r.slot_id,
            "corridor_id": r.corridor_id,
            "start_time": r.start_time,
            "end_time": r.end_time,
            "status": r.status,
            "derived_from": r.derived_from,
            "horizon": r.horizon,
            "duration_hours": round((r.end_time - r.start_time).total_seconds() / 3600.0, 2),
        }
        for r in rows
    ]


@router.post("/api/corridor-availability")
def create_corridor_availability(
    payload: schemas.CorridorAvailabilityCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=400, detail="end_time must be after start_time")

    slot = models.CorridorSlot(
        slot_id=f"slot-{uuid.uuid4().hex[:8]}",
        corridor_id=payload.corridor_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        status="available",
        derived_from="manual",
        horizon=payload.horizon,
    )
    db.add(slot)
    db.commit()
    db.refresh(slot)
    log(db, "corridor_slot_manual_add", current_user.user_id, {"slot_id": slot.slot_id, "corridor_id": slot.corridor_id})
    return {
        "slot_id": slot.slot_id,
        "corridor_id": slot.corridor_id,
        "start_time": slot.start_time,
        "end_time": slot.end_time,
        "status": slot.status,
        "derived_from": slot.derived_from,
        "horizon": slot.horizon,
    }


@router.post("/api/corridor-availability/derive")
def derive_corridor_availability(
    corridor_id: str,
    date_from: dt.date = None,
    date_to: dt.date = None,
    buffer_minutes: int = vacancy.DEFAULT_BUFFER_MINUTES,
    min_window_hours: float = vacancy.DEFAULT_MIN_WINDOW_HOURS,
    horizon: str = "weekly",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    """Fix 3: computes REAL corridor vacancy from actual train movements in
    the loaded timetable — every section of the corridor must be
    simultaneously free, with a safety buffer around each movement — and
    stores the result as CorridorSlot rows carrying the bracketing trains.

    date_from/date_to are IST calendar dates (default: today IST through
    +6 days). Fully re-runnable: previously derived slots in that range are
    replaced, never duplicated; manually-added windows are left untouched.
    Uses ONLY the static timetable — no live API dependency."""
    if horizon not in HORIZON_DAYS:
        raise HTTPException(status_code=400, detail="horizon must be 'weekly' or 'monthly'")
    if buffer_minutes < 0:
        raise HTTPException(status_code=400, detail="buffer_minutes must be >= 0")
    if min_window_hours <= 0:
        raise HTTPException(status_code=400, detail="min_window_hours must be > 0")

    date_from = date_from or ist_today()
    date_to = date_to or (date_from + dt.timedelta(days=HORIZON_DAYS[horizon] - 1))

    section_ids = corridor_builder.get_section_ids(db, corridor_id)
    has_trains = (
        db.query(models.TrainTimetableEntry)
        .filter(models.TrainTimetableEntry.corridor_id.in_(section_ids))
        .first()
    )
    if not has_trains:
        raise HTTPException(
            status_code=404,
            detail=f"no timetable entries loaded for corridor '{corridor_id}' (sections: {section_ids}). Load the "
            "timetable first, or check /api/corridors for corridor IDs that actually carry real train movements.",
        )

    try:
        result = vacancy.derive_and_store(
            db, corridor_id, date_from, date_to,
            buffer_minutes=buffer_minutes, min_window_hours=min_window_hours, horizon=horizon,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    log(
        db, "corridor_availability_derived", current_user.user_id,
        {"corridor_id": corridor_id, "date_from_ist": date_from.isoformat(), "date_to_ist": date_to.isoformat(),
         "buffer_minutes": buffer_minutes, "windows_found": result["windows_found"],
         "sections": result["section_count"]},
    )
    return result


@router.get("/api/corridor-availability/verify")
def verify_corridor_availability(
    corridor_id: str,
    date_from: dt.date = None,
    date_to: dt.date = None,
    buffer_minutes: int = vacancy.DEFAULT_BUFFER_MINUTES,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Independent correctness check: re-expands every real train movement
    on every section of the corridor and asserts no stored vacancy window
    overlaps any of them (buffer included). Deliberately implemented as a
    separate naive scan rather than reusing the derivation code — a check
    that shares the code it checks proves nothing."""
    date_from = date_from or ist_today()
    date_to = date_to or (date_from + dt.timedelta(days=6))
    return vacancy.verify_no_overlap(db, corridor_id, date_from, date_to, buffer_minutes)


@router.post("/api/corridor-availability/derive-ml-forecast")
def derive_ml_forecast_availability(
    payload: schemas.DeriveAvailabilityRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    """Layer 3B -> Layer 4A: converts the traffic-forecasting model's
    predicted low-traffic windows into CorridorSlot candidates, tagged
    derived_from='ml_forecast' so their provenance is always visible and
    distinguishable from a real derived traffic gap. These candidates then
    flow into the CP-SAT optimizer through the exact same get_safe_slots()
    path as every other slot — no scheduler change was needed to wire this
    in, because a slot is a slot regardless of how it was derived, and every
    slot is still independently re-validated against the real timetable
    before the solver ever sees it (see scheduler._slot_is_safe)."""
    from ml import traffic_forecast

    try:
        result = traffic_forecast.forecast_and_find_low_traffic_windows(
            db, payload.corridor_id, horizon=payload.horizon, min_gap_hours=payload.min_gap_hours
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    db.query(models.CorridorSlot).filter(
        models.CorridorSlot.corridor_id == payload.corridor_id,
        models.CorridorSlot.horizon == payload.horizon,
        models.CorridorSlot.derived_from == "ml_forecast",
    ).delete()

    created = []
    for w in result["windows"]:
        slot = models.CorridorSlot(
            slot_id=f"slot-mlf-{uuid.uuid4().hex[:8]}",
            corridor_id=payload.corridor_id,
            start_time=w["start"],
            end_time=w["end"],
            status="available",
            derived_from="ml_forecast",
            horizon=payload.horizon,
        )
        db.add(slot)
        created.append({"slot_id": slot.slot_id, "start_time": w["start"], "end_time": w["end"], "duration_hours": w["duration_hours"]})
    db.commit()

    log(db, "ml_forecast_availability_derived", current_user.user_id, {"corridor_id": payload.corridor_id, "horizon": payload.horizon, "windows_found": len(created)})

    result["slots_created"] = created
    return result


@router.get("/api/freight-forecast")
def list_freight_forecast(corridor_id: str = None, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    query = db.query(models.FreightForecastEntry)
    if corridor_id:
        query = query.filter(models.FreightForecastEntry.corridor_id == corridor_id)
    rows = query.order_by(models.FreightForecastEntry.forecast_window_start).all()
    return [
        {
            "id": r.id,
            "corridor_id": r.corridor_id,
            "forecast_window_start": r.forecast_window_start,
            "forecast_window_end": r.forecast_window_end,
            "expected_goods_traffic": r.expected_goods_traffic,
            "source": r.source,
        }
        for r in rows
    ]


@router.post("/api/freight-forecast")
def create_freight_forecast(
    payload: schemas.FreightForecastCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    row = models.FreightForecastEntry(
        corridor_id=payload.corridor_id,
        forecast_window_start=payload.forecast_window_start,
        forecast_window_end=payload.forecast_window_end,
        expected_goods_traffic=payload.expected_goods_traffic,
        source="manual",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log(db, "freight_forecast_added", current_user.user_id, {"corridor_id": row.corridor_id})
    return {"id": row.id, "corridor_id": row.corridor_id}
