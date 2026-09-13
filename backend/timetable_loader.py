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
from tz_utils import IST, UTC, ist_date_to_utc_bounds, to_ist

RAW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw")
STATIONS_PATH = os.path.join(RAW_DIR, "stations.json")
SCHEDULES_PATH = os.path.join(RAW_DIR, "schedules.json")
TRAINS_PATH = os.path.join(RAW_DIR, "trains.json")  # real service type/zone per train number

SOURCE_LABEL = "datameet_github_mirror_of_data_gov_in"
SOURCE_URL = "https://raw.githubusercontent.com/datameet/railways/master/schedules.json"

# Arbitrary anchor date. Only the time-of-day (and whether arrival rolled
# into the next stored day, for overnight legs) is ever read back out.
ANCHOR_DATE = dt.date(2000, 1, 1)

# TIMEZONE CORRECTNESS: the real published Indian Railways timetable times
# in the source dataset (e.g. "22:00:00") are IST wall-clock times — that is
# the only sensible reading of a real published Indian train timetable, and
# the whole point of this system correctly reflecting real train movements
# depends on it. Every scheduled_departure/scheduled_arrival stored below is
# therefore explicitly converted from "this time-of-day, in IST" to its
# UTC-equivalent instant before being written to the DB — matching this
# codebase's uniform storage contract (see tz_utils.py) that every stored
# datetime is a UTC instant. Getting this wrong would silently shift every
# real train's displayed time by exactly 5.5 hours once IST-correct display
# was added elsewhere — this is precisely the bug this conversion prevents.


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


# NOTE: station loading lives in station_loader.py, not here. The version
# that used to sit at this spot did a DELETE-ALL-then-reinsert and dropped
# the source's state/address fields entirely; station_loader.load_stations()
# replaces it with an idempotent, multi-source, gap-reporting upsert.


def _train_metadata() -> dict:
    """train_number -> {service_type, zone} from the real trains.json in the
    same DataMeet dataset. The schedule rows carry no service class, so
    service_type was previously stored empty for every segment; this fills
    it with the dataset's own real `type` value (EXP / DEMU / MEMU / SF /
    PASS ...). Missing file is tolerated — the timetable still loads, just
    without service types, rather than failing or inventing one."""
    if not os.path.exists(TRAINS_PATH):
        return {}
    try:
        with open(TRAINS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}
    feats = data["features"] if isinstance(data, dict) else data
    out = {}
    for feat in feats:
        props = (feat.get("properties") or {}) if isinstance(feat, dict) else {}
        number = props.get("number")
        if number:
            out[str(number)] = {"service_type": props.get("type") or "", "zone": props.get("zone") or ""}
    return out


def load_timetable(db: Session) -> dict:
    """Parses the full real schedule dataset into TrainTimetableEntry segments."""
    with open(SCHEDULES_PATH, encoding="utf-8") as f:
        schedule_rows = json.load(f)

    train_meta = _train_metadata()

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
            # Build the IST wall-clock instant first (overnight rollover is
            # an IST calendar concept — a train published as departing
            # 23:50 and arriving 00:10 crosses midnight IST, not UTC
            # midnight), THEN convert to the UTC instant actually stored.
            dep_dt_ist = dt.datetime.combine(ANCHOR_DATE, dep_t, tzinfo=IST)
            arr_dt_ist = dt.datetime.combine(ANCHOR_DATE, arr_t, tzinfo=IST)
            if arr_dt_ist <= dep_dt_ist:
                arr_dt_ist += dt.timedelta(days=1)  # overnight leg
            dep_dt = dep_dt_ist.astimezone(UTC).replace(tzinfo=None)
            arr_dt = arr_dt_ist.astimezone(UTC).replace(tzinfo=None)
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
                    "service_type": train_meta.get(str(train_number), {}).get("service_type", ""),
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
    occurrences of real train movements across the requested horizon.

    horizon_start is an IST CALENDAR date (see tz_utils.ist_today —
    "this week" for an Indian railway corridor means the IST week, so the
    day-by-day expansion below must walk IST calendar days). Each template's
    real time-of-day is itself IST (see the load_timetable() note above), so
    it's read back out in IST (to_ist(...).time()) before being recombined
    with the IST calendar date being expanded — combining a UTC time-of-day
    with an IST calendar date would silently misplace any train whose IST
    time, once shifted to UTC, crosses a calendar-day boundary (exactly the
    near-midnight bug this whole fix targets). The final instant is
    converted back to UTC for storage-comparable output, since every other
    naive datetime this function's callers compare against (CorridorSlot
    times, etc.) is naive-but-UTC by this codebase's uniform contract."""
    templates = (
        db.query(models.TrainTimetableEntry)
        .filter(models.TrainTimetableEntry.corridor_id == corridor_id)
        .all()
    )
    occurrences = []
    for t in templates:
        duration = t.scheduled_arrival - t.scheduled_departure  # a span, timezone-invariant
        dep_time_ist = to_ist(t.scheduled_departure).time()
        for day_index in range(horizon_days):
            ist_date = horizon_start + dt.timedelta(days=day_index)
            dep_ist = dt.datetime.combine(ist_date, dep_time_ist, tzinfo=IST)
            dep = dep_ist.astimezone(UTC).replace(tzinfo=None)
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


def train_occurrences_on_date(db: Session, corridor_id: str, ist_date: dt.date):
    """Like corridor_occurrences, but for exactly one IST calendar date and
    returning full per-train detail (station codes, service_type) rather
    than just start/end — what the corridor-search feature (§10) needs.
    Reuses the identical duration-preserving IST-expansion approach as
    corridor_occurrences so the two never disagree about what "this IST
    date" means for a given train."""
    templates = (
        db.query(models.TrainTimetableEntry)
        .filter(models.TrainTimetableEntry.corridor_id == corridor_id)
        .all()
    )
    rows = []
    for t in templates:
        duration = t.scheduled_arrival - t.scheduled_departure
        dep_time_ist = to_ist(t.scheduled_departure).time()
        dep_ist = dt.datetime.combine(ist_date, dep_time_ist, tzinfo=IST)
        dep_utc = dep_ist.astimezone(UTC).replace(tzinfo=None)
        arr_utc = dep_utc + duration
        rows.append(
            {
                "train_id": t.train_id,
                "train_name": t.train_name,
                "from_station_code": t.from_station_code,
                "to_station_code": t.to_station_code,
                "corridor_id": t.corridor_id,
                "service_type": t.service_type,
                "departure": dep_utc,
                "arrival": arr_utc,
            }
        )
    rows.sort(key=lambda r: r["departure"])
    return rows


# NOTE: gap derivation lives in vacancy.py, not here. The derive_gaps()
# that used to sit at this spot computed single-section gaps with NO safety
# buffer and no multi-section intersection; vacancy.compute_vacancy()
# replaces it with the real operational definition (buffered occupancy,
# every section simultaneously free, bracketing trains recorded).
