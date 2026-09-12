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
    return [{"station_code": r.station_code, "station_name": r.station_name, "zone": r.zone} for r in rows]


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
