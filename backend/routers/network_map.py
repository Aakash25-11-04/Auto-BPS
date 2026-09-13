"""Live railway network map endpoints (Feature 11).

RBAC: every authenticated role can view the map (it is a shared operational
picture). Task-level block detail is row-scoped exactly like
/api/schedule/plan — see map_service's module docstring.

All four endpoints are read-only aggregations: no optimizer, no writes. The
two FeatureCollection endpoints return pre-serialized JSON (see
map_service for why) and report their own build time in meta.build_ms.
"""
import datetime as dt
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

import auth
import map_service
import models
from database import get_db
from tz_utils import ist_today

router = APIRouter(prefix="/api/map", tags=["map"])

HORIZONS = ("weekly", "monthly")


def _parse_bbox(bbox: str):
    if not bbox:
        return None
    try:
        parts = [float(x) for x in bbox.split(",")]
    except ValueError:
        parts = []
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox must be 'minLon,minLat,maxLon,maxLat'")
    return parts


def _check_horizon(horizon: str):
    if horizon not in HORIZONS:
        raise HTTPException(status_code=400, detail="horizon must be 'weekly' or 'monthly'")


def _json(payload) -> Response:
    content = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    return Response(content=content, media_type="application/json")


@router.get("/corridors")
def map_corridors(
    date: dt.date = None,
    horizon: str = "weekly",
    include_clear: bool = True,
    bbox: str = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """GeoJSON FeatureCollection of real corridor polylines, each carrying its
    status for `date` (IST; default today): clear | upcoming | active |
    completed, the featured active/upcoming block (departments, task count,
    IST times, gang assignments) and today_summary. include_clear=false
    returns only corridors with blocks that day — what the map's 60s
    auto-refresh polls, since the clear network's geometry never changes."""
    _check_horizon(horizon)
    return _json(map_service.corridors_geojson(
        db, date or ist_today(), horizon, current_user, include_clear=include_clear, bbox=_parse_bbox(bbox),
    ))


@router.get("/stations")
def map_stations(
    date: dt.date = None,
    horizon: str = "weekly",
    bbox: str = None,
    include_quiet: bool = True,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """GeoJSON FeatureCollection of every station with real coordinates:
    code, name, zone, station_type, map tier (major/regular/halt) and
    today_activity (blocks touching the station on `date`). include_quiet=false
    returns only stations with activity — the refresh poll's lightweight form."""
    _check_horizon(horizon)
    return _json(map_service.stations_geojson(db, date or ist_today(), horizon, current_user, bbox=_parse_bbox(bbox), include_quiet=include_quiet))


@router.get("/corridors/{corridor_id}")
def map_corridor_detail(
    corridor_id: str,
    date: dt.date = None,
    horizon: str = "weekly",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Corridor popup: stations, distance, every block that day (with gang
    assignments) and a weather summary."""
    _check_horizon(horizon)
    try:
        return _json(map_service.corridor_detail(db, corridor_id, date or ist_today(), horizon, current_user))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/stations/{station_code}")
def map_station_detail(
    station_code: str,
    date: dt.date = None,
    horizon: str = "weekly",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Station popup: real trains through the station today (count + next 3
    departures, IST), blocks affecting it, and weather."""
    _check_horizon(horizon)
    try:
        return _json(map_service.station_detail(db, station_code, date or ist_today(), horizon, current_user))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
