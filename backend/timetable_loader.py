"""Real-data ingestion for the train timetable and derived corridor availability.

Source: the DataMeet "Indian Railways" open dataset (CC0), a mirror of the
data.gov.in Indian Railways Train Time Table catalog, gathered from
data.gov.in per its own README (https://github.com/datameet/railways).
We fetch it directly from GitHub raw content because data.gov.in itself does
not expose a stable, unauthenticated bulk-download URL for this catalog.

Each schedule row is one train's stop at one station. Consecutive stops of
the same train (ordered by the dataset's own monotonic `id` field, which is
assigned in stop sequence) define one corridor segment: a real
from-station -> to-station leg with real scheduled departure/arrival times.

The source has no weekly running-days calendar (most Indian Railways trains
run daily and the open dataset does not distinguish exceptions), so each
segment is treated as recurring once per calendar day within whatever
horizon it is expanded over. This assumption is stated here and in the
README rather than silently baked in.
"""
import datetime as dt
import json
import os
from collections import defaultdict

from sqlalchemy.orm import Session

import models

RAW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw")
STATIONS_PATH = os.path.join(RAW_DIR, "stations.json")
SCHEDULES_PATH = os.path.join(RAW_DIR, "schedules.json")

SOURCE_LABEL = "datameet_github_mirror_of_data_gov_in"
SOURCE_URL = "https://raw.githubusercontent.com/datameet/railways/master/schedules.json"

# Arbitrary anchor date. Only the time-of-day (and whether arrival rolled
# into the next stored day, for overnight legs) is ever read back out.
ANCHOR_DATE = dt.date(2000, 1, 1)


def _parse_time(value: str):
    if not value or value == "None":
        return None
    try:
        h, m, s = value.split(":")
        return dt.time(int(h), int(m), int(s))
    except ValueError:
        return None


def files_present() -> bool:
    return os.path.exists(STATIONS_PATH) and os.path.exists(SCHEDULES_PATH)


def load_stations(db: Session) -> int:
    with open(STATIONS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    feats = data["features"] if isinstance(data, dict) else data

    db.query(models.Station).delete()
    rows = []
    seen = set()
    for feat in feats:
        props = feat.get("properties", {})
        code = props.get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") if geom.get("type") == "Point" else None
        lon, lat = (coords[0], coords[1]) if coords and len(coords) == 2 else (None, None)
        rows.append(
            {
                "station_code": code,
                "station_name": props.get("name") or code,
                "zone": props.get("zone") or "",
                "lat": lat,
                "lon": lon,
            }
        )
    db.bulk_insert_mappings(models.Station, rows)
    db.commit()
    return len(rows)


def load_timetable(db: Session) -> dict:
    """Parses the full real schedule dataset into TrainTimetableEntry segments."""
    with open(SCHEDULES_PATH, encoding="utf-8") as f:
        schedule_rows = json.load(f)

    by_train_day = defaultdict(list)
    for row in schedule_rows:
        by_train_day[(row["train_number"], row["day"])].append(row)

    db.query(models.TrainTimetableEntry).delete()

    entries = []
    sample_trains = set()
    for (train_number, _day), stops in by_train_day.items():
        stops.sort(key=lambda r: r["id"])
        for a, b in zip(stops, stops[1:]):
            dep_t = _parse_time(a.get("departure"))
            arr_t = _parse_time(b.get("arrival"))
            if dep_t is None or arr_t is None:
                continue
            dep_dt = dt.datetime.combine(ANCHOR_DATE, dep_t)
            arr_dt = dt.datetime.combine(ANCHOR_DATE, arr_t)
            if arr_dt <= dep_dt:
                arr_dt += dt.timedelta(days=1)  # overnight leg
            corridor_id = f"{a['station_code']}-{b['station_code']}"
            entries.append(
                {
                    "train_id": train_number,
                    "train_name": a.get("train_name", ""),
                    "from_station_code": a["station_code"],
                    "to_station_code": b["station_code"],
                    "corridor_id": corridor_id,
                    "scheduled_departure": dep_dt,
                    "scheduled_arrival": arr_dt,
                    "service_type": "",
                    "source": SOURCE_LABEL,
                }
            )
            sample_trains.add(train_number)

    # bulk insert in chunks to keep memory/SQLite happy
    CHUNK = 20000
    for i in range(0, len(entries), CHUNK):
        db.bulk_insert_mappings(models.TrainTimetableEntry, entries[i : i + CHUNK])
    db.commit()

    distinct_corridors = len({e["corridor_id"] for e in entries})
    return {
        "segments_loaded": len(entries),
        "distinct_trains": len(sample_trains),
        "distinct_corridors": distinct_corridors,
        "sample_train_numbers": sorted(list(sample_trains))[:8],
        "source": SOURCE_LABEL,
        "source_url": SOURCE_URL,
    }


def top_corridors(db: Session, limit: int = 15):
    from sqlalchemy import func

    rows = (
        db.query(models.TrainTimetableEntry.corridor_id, func.count().label("n"))
        .group_by(models.TrainTimetableEntry.corridor_id)
        .order_by(func.count().desc())
        .limit(limit)
        .all()
    )
    return [{"corridor_id": r[0], "segment_count": r[1]} for r in rows]


def corridor_occurrences(db: Session, corridor_id: str, horizon_start: dt.date, horizon_days: int):
    """Expand recurring daily segment templates into concrete datetime
    occurrences of real train movements across the requested horizon."""
    templates = (
        db.query(models.TrainTimetableEntry)
        .filter(models.TrainTimetableEntry.corridor_id == corridor_id)
        .all()
    )
    occurrences = []
    for t in templates:
        duration = t.scheduled_arrival - t.scheduled_departure
        dep_time = t.scheduled_departure.time()
        for day_index in range(horizon_days):
            date = horizon_start + dt.timedelta(days=day_index)
            dep = dt.datetime.combine(date, dep_time)
            arr = dep + duration
            occurrences.append(
                {
                    "train_id": t.train_id,
                    "train_name": t.train_name,
                    "start": dep,
                    "end": arr,
                }
            )
    occurrences.sort(key=lambda o: o["start"])
    return occurrences


def derive_gaps(db: Session, corridor_id: str, horizon_start: dt.date, horizon_days: int, min_gap_hours: float):
    """Finds genuine traffic gaps on a corridor: windows with no scheduled
    train movement, bracketed by the real trains on either side."""
    occurrences = corridor_occurrences(db, corridor_id, horizon_start, horizon_days)
    horizon_end = dt.datetime.combine(horizon_start, dt.time.min) + dt.timedelta(days=horizon_days)
    horizon_begin = dt.datetime.combine(horizon_start, dt.time.min)

    if not occurrences:
        return [], occurrences

    # merge overlapping/adjacent occupied windows
    merged = []
    for occ in occurrences:
        if merged and occ["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], occ["end"])
            merged[-1]["after_train"] = occ["train_id"]
        else:
            merged.append(
                {
                    "start": occ["start"],
                    "end": occ["end"],
                    "before_train": occ["train_id"],
                    "after_train": occ["train_id"],
                }
            )

    gaps = []
    min_gap = dt.timedelta(hours=min_gap_hours)

    cursor = horizon_begin
    prev_train = None
    for block in merged:
        if block["start"] > cursor:
            gap_len = block["start"] - cursor
            if gap_len >= min_gap:
                gaps.append(
                    {
                        "start": cursor,
                        "end": block["start"],
                        "preceding_train": prev_train,
                        "following_train": block["before_train"],
                    }
                )
        cursor = max(cursor, block["end"])
        prev_train = block["after_train"]

    if horizon_end > cursor:
        gap_len = horizon_end - cursor
        if gap_len >= min_gap:
            gaps.append(
                {
                    "start": cursor,
                    "end": horizon_end,
                    "preceding_train": prev_train,
                    "following_train": None,
                }
            )

    return gaps, occurrences
