"""Station master loading (Fix 1) — real Indian Railways stations, merged
from open datasets, never invented.

SOURCES, in the priority order they are merged (a later source only FILLS
GAPS in an already-loaded station; it never overwrites a field that a
higher-priority source already supplied):

  1. datameet/railways  (data/raw/stations.json, already vendored)
     https://github.com/datameet/railways
     GeoJSON FeatureCollection: code, name, state, zone, address + real
     Point coordinates. The most complete public Indian station dataset
     with coordinates — 8,990 stations on this build.

  2. vstflugel/indian-railway-dataset (fetched over HTTP when reachable)
     https://github.com/vstflugel/indian-railway-dataset
     list_of_stations.json: station_code, station_name, region_code. No
     coordinates, so it is used ONLY to add stations datameet is missing
     and to fill blank names/zones — this is what closes real gaps like
     MMCT (Mumbai Central), which datameet carries under the code BCT.

If a network source is unreachable the load still succeeds with whatever
local sources provided and REPORTS the failure explicitly (URL + error) —
it never substitutes invented stations to hit a target count.

IDEMPOTENT: this is an UPSERT, not a delete-and-reload. Re-running it
updates existing rows in place and leaves unrelated data (and any station
a previous run added from a source that is currently unreachable) intact.
"""
import datetime as dt
import json
import os
from typing import Dict

from sqlalchemy.orm import Session

import models
from tz_utils import utc_now

RAW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw")
STATIONS_PATH = os.path.join(RAW_DIR, "stations.json")

DATAMEET_STATIONS_URL = "https://raw.githubusercontent.com/datameet/railways/master/stations.json"
# Verified against the repo's actual tree (GitHub API) — the file sits at the
# repository ROOT, not under data/. 13,147 stations with code/name/region.
VSTFLUGEL_STATIONS_URL = "https://raw.githubusercontent.com/vstflugel/indian-railway-dataset/main/list_of_stations.json"

# Real alternate station codes. Some codes in everyday use (NTES/IRCTC
# booking codes) differ from the codes the open geographic datasets carry,
# so a user searching the familiar one would otherwise get "no such
# station" even though the station IS loaded. These are ALIASES onto real
# loaded stations — no station row is ever invented to satisfy a lookup.
# Verified individually against the loaded master before being listed here.
STATION_CODE_ALIASES = {
    "MMCT": "BCT",    # Mumbai Central — MMCT is the IRCTC/NTES code, BCT the geographic-dataset code
    "CSMT": "CSTM",   # Chhatrapati Shivaji Maharaj Terminus (renamed 2017)
    "MAS": "MAS",     # Chennai Central (present under MAS in both)
    "SBC": "SBC",     # KSR Bengaluru City Jn
}


def resolve_alias(code: str) -> str:
    """Maps a well-known alternate code onto the code actually loaded."""
    return STATION_CODE_ALIASES.get((code or "").strip().upper(), (code or "").strip().upper())

# The open sources carry no explicit station_type field, so it is derived
# from the real station NAME by this documented heuristic. It is used to
# pick natural corridor endpoints (see corridor_builder.py) — junctions and
# terminals are where railway staff actually delimit a stretch of line.
def classify_station(name: str) -> str:
    n = (name or "").upper()
    if "JN" in n.split() or "JUNCTION" in n or n.endswith(" JN"):
        return "junction"
    if "TERMINUS" in n or "TERMINAL" in n or n.endswith(" T"):
        return "terminal"
    if "HALT" in n or n.endswith(" H"):
        return "halt"
    if "CABIN" in n or "BLOCK HUT" in n or "SIDING" in n:
        return "cabin"
    return "regular"


def _upsert(db: Session, existing: Dict[str, models.Station], code: str, fields: dict, source: str, stats: dict):
    """Fill-gaps-only upsert: an existing non-empty value is never clobbered
    by a lower-priority source, so re-running with a partial source can only
    ever ADD information."""
    code = (code or "").strip().upper()
    if not code:
        return
    row = existing.get(code)
    if row is None:
        row = models.Station(station_code=code, station_name=fields.get("station_name") or code, source=source)
        db.add(row)
        existing[code] = row
        stats["created"] += 1
    else:
        stats["updated"] += 1
    for key, value in fields.items():
        if value in (None, ""):
            continue
        current = getattr(row, key, None)
        if current in (None, "", 0):
            setattr(row, key, value)
    if not row.station_type or row.station_type == "regular":
        row.station_type = classify_station(row.station_name)
    row.updated_at = utc_now()


def _load_datameet(db: Session, existing: dict, stats: dict) -> dict:
    if not os.path.exists(STATIONS_PATH):
        return {"source": "datameet", "loaded": 0, "error": f"local file missing: {STATIONS_PATH}"}
    with open(STATIONS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    feats = data["features"] if isinstance(data, dict) else data
    n = 0
    for feat in feats:
        props = feat.get("properties", {}) or {}
        code = props.get("code")
        if not code:
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") if geom.get("type") == "Point" else None
        lon, lat = (coords[0], coords[1]) if coords and len(coords) == 2 else (None, None)
        _upsert(
            db, existing, code,
            {
                "station_name": props.get("name") or code,
                "state": props.get("state") or "",
                "zone": props.get("zone") or "",
                "address": props.get("address") or "",
                "lat": lat,
                "lon": lon,
            },
            source="datameet/railways", stats=stats,
        )
        n += 1
    return {"source": "datameet/railways", "loaded": n, "error": None}


def _load_vstflugel(db: Session, existing: dict, stats: dict, timeout: int = 20) -> dict:
    """Supplementary gap-filler, fetched live. Reports its own failure
    rather than being silently skipped — an unreachable source must be
    visible in the load report, never papered over."""
    import requests

    try:
        resp = requests.get(VSTFLUGEL_STATIONS_URL, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return {"source": "vstflugel/indian-railway-dataset", "loaded": 0, "url": VSTFLUGEL_STATIONS_URL, "error": str(e)}

    rows = payload if isinstance(payload, list) else payload.get("stations", payload.get("data", []))
    n = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = r.get("station_code") or r.get("code") or r.get("stationCode")
        if not code:
            continue
        _upsert(
            db, existing, code,
            {
                "station_name": r.get("station_name") or r.get("name") or "",
                "zone": r.get("region_code") or r.get("zone") or "",
                "state": r.get("state") or "",
            },
            source="vstflugel/indian-railway-dataset", stats=stats,
        )
        n += 1
    return {"source": "vstflugel/indian-railway-dataset", "loaded": n, "url": VSTFLUGEL_STATIONS_URL, "error": None}


def load_stations(db: Session, include_remote: bool = True) -> dict:
    """Merges every available source into the station master and returns a
    full, honest report — including which sources failed and which station
    codes the loaded timetable references but the master does not have
    (the real completeness gap, flagged rather than hidden)."""
    existing = {s.station_code: s for s in db.query(models.Station).all()}
    stats = {"created": 0, "updated": 0}
    sources = [_load_datameet(db, existing, stats)]
    if include_remote:
        sources.append(_load_vstflugel(db, existing, stats))
    db.commit()

    total = db.query(models.Station).count()
    with_coords = (
        db.query(models.Station)
        .filter(models.Station.lat.isnot(None), models.Station.lon.isnot(None))
        .count()
    )
    from sqlalchemy import func

    by_type = {
        t or "regular": c
        for (t, c) in db.query(models.Station.station_type, func.count(models.Station.station_code))
        .group_by(models.Station.station_type)
        .all()
    }

    # The real completeness check: every station the loaded timetable
    # actually references must exist in the master, or scheduling against
    # that station silently degrades.
    tt_codes = {r[0] for r in db.query(models.TrainTimetableEntry.from_station_code).distinct().all()}
    tt_codes |= {r[0] for r in db.query(models.TrainTimetableEntry.to_station_code).distinct().all()}
    master_codes = set(existing.keys())
    missing_from_master = sorted(tt_codes - master_codes)

    return {
        "sources": sources,
        "stations_created": stats["created"],
        "stations_updated": stats["updated"],
        "total_stations": total,
        "by_station_type": by_type,
        "with_coordinates": with_coords,
        "without_coordinates": total - with_coords,
        "timetable_station_codes": len(tt_codes),
        "timetable_codes_missing_from_master": missing_from_master,
        "timetable_codes_missing_count": len(missing_from_master),
    }
