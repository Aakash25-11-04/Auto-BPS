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
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import models
import schemas
import timetable_loader
from audit import log
from database import get_db

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
    stations_count = timetable_loader.load_stations(db)
    result = timetable_loader.load_timetable(db)
    result["stations_loaded"] = stations_count
    log(db, "timetable_loaded", current_user.user_id, result)
    return result


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
    payload: schemas.DeriveAvailabilityRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    if payload.horizon not in HORIZON_DAYS:
        raise HTTPException(status_code=400, detail="horizon must be 'weekly' or 'monthly'")

    has_trains = (
        db.query(models.TrainTimetableEntry)
        .filter(models.TrainTimetableEntry.corridor_id == payload.corridor_id)
        .first()
    )
    if not has_trains:
        raise HTTPException(
            status_code=404,
            detail=f"no timetable entries loaded for corridor '{payload.corridor_id}'. Load the timetable first, "
            "or check /api/timetable/top-corridors for corridor IDs that actually have real train movements.",
        )

    horizon_start = payload.start_date or dt.date.today()
    horizon_days = HORIZON_DAYS[payload.horizon]
    gaps, occurrences = timetable_loader.derive_gaps(
        db, payload.corridor_id, horizon_start, horizon_days, payload.min_gap_hours
    )

    # clear previously-derived (not manual) slots for this corridor+horizon before re-deriving
    db.query(models.CorridorSlot).filter(
        models.CorridorSlot.corridor_id == payload.corridor_id,
        models.CorridorSlot.horizon == payload.horizon,
        models.CorridorSlot.derived_from == "timetable_gap",
    ).delete()

    created = []
    for gap in gaps:
        slot = models.CorridorSlot(
            slot_id=f"slot-{uuid.uuid4().hex[:8]}",
            corridor_id=payload.corridor_id,
            start_time=gap["start"],
            end_time=gap["end"],
            status="available",
            derived_from="timetable_gap",
            horizon=payload.horizon,
        )
        db.add(slot)
        created.append(
            {
                "slot_id": slot.slot_id,
                "start_time": gap["start"],
                "end_time": gap["end"],
                "duration_hours": round((gap["end"] - gap["start"]).total_seconds() / 3600.0, 2),
                "preceding_train": gap["preceding_train"],
                "following_train": gap["following_train"],
            }
        )
    db.commit()

    log(
        db,
        "corridor_availability_derived",
        current_user.user_id,
        {"corridor_id": payload.corridor_id, "horizon": payload.horizon, "gaps_found": len(created)},
    )

    return {
        "corridor_id": payload.corridor_id,
        "horizon": payload.horizon,
        "horizon_start": horizon_start,
        "horizon_days": horizon_days,
        "real_train_movements_considered": len(occurrences),
        "gaps_found": len(created),
        "slots": created,
    }


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
