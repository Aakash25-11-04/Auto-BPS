"""Live railway network map data (Feature 11) — read-only aggregations.

Everything here is a READ over data other layers already own: real station
coordinates (station_loader), real corridor polylines (corridor_builder),
the current block plan, and (optionally) Feature 10's gang assignments. No
optimizer involvement and no writes, so both GeoJSON endpoints stay fast.

Performance design (both FeatureCollection endpoints must answer < 500ms):
  - The STATIC part — ~17k corridor polylines and ~8.7k geolocated
    stations — is parsed from the DB once and cached in process memory,
    keyed by a cheap fingerprint (row count + max timestamp + total geometry
    length), so a corridor rebuild or station reload invalidates it
    automatically.
  - The DYNAMIC part — today's blocks for the chosen date — is one indexed
    query over the plan's entries for that IST day.
  - Responses are serialized with json.dumps directly (not FastAPI's
    per-field jsonable_encoder, which is far slower on tens of thousands of
    nested dicts) and gzip-compressed by the app middleware.

Country-view station tier: the station master's station_type comes from a
NAME heuristic ("... JN" -> junction), which labels New Delhi, Chennai
Central and Mumbai CST as 'regular'. The map's "major" tier is therefore
junction/terminal PLUS any station the real timetable shows at least
MAJOR_TERMINUS_MIN_TRAINS trains starting or ending at — a de facto
terminal. That count is derived once from the timetable and cached in
AppSetting (it takes a few seconds on the full 386k-row dataset).

Department scoping mirrors /schedule/plan: a department role sees every
block's department code and times (needed to read the map at all), but
task-level detail (task id, defect, asset, gang) only for its OWN tasks.
"""
import datetime as dt
import json
import math
import threading
import time
from collections import defaultdict

from sqlalchemy import func

import models
from tz_utils import ist_date_to_utc_bounds, ist_today, to_ist, utc_iso, utc_now

try:  # Feature 10 is optional: the map works (without gang labels) if it is removed
    import crew_capacity
except ImportError:  # pragma: no cover
    crew_capacity = None

COORD_DECIMALS = 5  # ~1 m — far finer than a station marker, and keeps payloads small
MAJOR_TERMINUS_MIN_TRAINS = 20
TERMINUS_SETTING_KEY = "map.terminus_train_counts"
DEPT_ROLES = ("ENG", "TD", "SNT")
DEPARTMENTS = ("ENG", "TD", "SNT")

# Station code renames the open dataset predates (it still uses the old
# code), so a lookup by the current official code still finds the station.
STATION_CODE_ALIASES = {"MMCT": "BCT"}

_lock = threading.Lock()
_cache = {"corridor_key": None, "corridors": None, "station_key": None, "stations": None, "corridors_by_station": None}
# The fingerprint queries themselves cost ~150ms on the full dataset (one
# counts 386k timetable rows), so they're re-checked at most this often.
# A corridor rebuild / station reload is therefore picked up within 30s.
FINGERPRINT_RECHECK_SECONDS = 30
_fingerprint_checked_at = {"corridor": 0.0, "station": 0.0}
_weather_cache = {}  # (lat, lon, date) -> (fetched_monotonic, summary)
LIVE_WEATHER_TTL_SECONDS = 30 * 60


# ============================================================ static geometry

def _round_coords(coords):
    return [[round(c[0], COORD_DECIMALS), round(c[1], COORD_DECIMALS)] for c in coords]


def _corridor_fingerprint(db):
    return tuple(db.query(
        func.count(models.Corridor.corridor_id), func.max(models.Corridor.created_at),
        func.sum(func.length(models.Corridor.geometry_json)),
    ).one())


def _station_fingerprint(db):
    return tuple(db.query(
        func.count(models.Station.station_code), func.max(models.Station.updated_at),
    ).one()) + (db.query(func.count(models.TrainTimetableEntry.id)).scalar(),)


def terminus_train_counts(db) -> dict:
    """{station_code: distinct trains that START or END there}, from the real
    timetable. A train's origin is a station it departs from but never
    arrives at; its destination the reverse. Cached in AppSetting against
    the timetable row count, since the full computation takes ~2.5s."""
    rows_now = db.query(func.count(models.TrainTimetableEntry.id)).scalar() or 0
    setting = db.query(models.AppSetting).filter_by(key=TERMINUS_SETTING_KEY).first()
    if setting:
        try:
            cached = json.loads(setting.value or "{}")
            if cached.get("timetable_rows") == rows_now:
                return cached.get("counts", {})
        except ValueError:
            pass
    sql = """
        WITH f AS (SELECT DISTINCT train_id, from_station_code AS s FROM train_timetable_entries),
             t AS (SELECT DISTINCT train_id, to_station_code AS s FROM train_timetable_entries),
             origin AS (SELECT f.train_id, f.s FROM f LEFT JOIN t ON t.train_id = f.train_id AND t.s = f.s WHERE t.s IS NULL),
             dest AS (SELECT t.train_id, t.s FROM t LEFT JOIN f ON f.train_id = t.train_id AND f.s = t.s WHERE f.s IS NULL),
             ends AS (SELECT s, train_id FROM origin UNION ALL SELECT s, train_id FROM dest)
        SELECT s, COUNT(DISTINCT train_id) FROM ends GROUP BY s
    """
    counts = {code: n for code, n in db.execute(_text(sql)).fetchall()}
    payload = json.dumps({"timetable_rows": rows_now, "counts": counts})
    if setting:
        setting.value = payload
    else:
        db.add(models.AppSetting(key=TERMINUS_SETTING_KEY, value=payload))
    db.commit()
    return counts


def _text(sql):
    from sqlalchemy import text
    return text(sql)


def _fresh_enough(kind: str) -> bool:
    return time.monotonic() - _fingerprint_checked_at[kind] < FINGERPRINT_RECHECK_SECONDS


def static_corridors(db) -> list:
    with _lock:
        if _cache["corridors"] is not None and _fresh_enough("corridor"):
            return _cache["corridors"]
    key = _corridor_fingerprint(db)
    with _lock:
        if _cache["corridor_key"] == key and _cache["corridors"] is not None:
            _fingerprint_checked_at["corridor"] = time.monotonic()
            return _cache["corridors"]
    rows = db.query(
        models.Corridor.corridor_id, models.Corridor.kind, models.Corridor.from_station_code,
        models.Corridor.to_station_code, models.Corridor.intermediate_stations, models.Corridor.geometry_json,
        models.Corridor.total_distance_km, models.Corridor.zone, models.Corridor.train_count,
        models.Corridor.section_count,
    ).all()
    out = []
    by_station = defaultdict(list)
    for cid, kind, a, b, inter, geom, dist, zone, trains, sections in rows:
        if not geom:
            continue
        try:
            coords = json.loads(geom).get("coordinates") or []
        except ValueError:
            continue
        if len(coords) < 2:
            continue
        stations = [a] + json.loads(inter or "[]") + [b]
        rec = {
            "corridor_id": cid, "kind": kind, "from_station": a, "to_station": b, "stations": stations,
            "coords": _round_coords(coords), "distance_km": round(dist, 2) if dist is not None else None,
            "zone": zone or "", "train_count": trains or 0, "section_count": sections or 1,
            "geometry_source": "corridor_master",
        }
        # A clear corridor's feature is identical on every request, so it is
        # serialized ONCE here and spliced into responses verbatim.
        rec["clear_json"] = json.dumps({
            "type": "Feature", "id": cid, "geometry": {"type": "LineString", "coordinates": rec["coords"]},
            "properties": {
                "corridor_id": cid, "kind": kind, "from_station": a, "to_station": b,
                "distance_km": rec["distance_km"], "zone": rec["zone"], "status": "clear", "active_block": None,
                "today_summary": {"total_blocks_today": 0, "total_hours_blocked": 0.0, "departments_involved": []},
            },
        }, separators=(",", ":"))
        out.append(rec)
        for code in stations:
            by_station[code].append(cid)
    with _lock:
        _fingerprint_checked_at["corridor"] = time.monotonic()
        _cache["corridor_key"] = key
        _cache["corridors"] = out
        _cache["corridor_index"] = {c["corridor_id"]: c for c in out}
        _cache["corridors_by_station"] = dict(by_station)
    return out


def corridor_index(db) -> dict:
    static_corridors(db)
    return _cache["corridor_index"]


def corridors_by_station(db) -> dict:
    static_corridors(db)
    return _cache["corridors_by_station"]


def static_stations(db) -> list:
    with _lock:
        if _cache["stations"] is not None and _fresh_enough("station"):
            return _cache["stations"]
    key = _station_fingerprint(db)
    with _lock:
        if _cache["station_key"] == key and _cache["stations"] is not None:
            _fingerprint_checked_at["station"] = time.monotonic()
            return _cache["stations"]
    termini = terminus_train_counts(db)
    rows = db.query(
        models.Station.station_code, models.Station.station_name, models.Station.zone, models.Station.state,
        models.Station.station_type, models.Station.lat, models.Station.lon,
    ).filter(models.Station.lat.isnot(None), models.Station.lon.isnot(None)).all()
    out = []
    for code, name, zone, state, stype, lat, lon in rows:
        n_term = termini.get(code, 0)
        stype = stype or "regular"
        if stype in ("junction", "terminal") or n_term >= MAJOR_TERMINUS_MIN_TRAINS:
            tier = "major"
        elif stype == "halt":
            tier = "halt"
        else:
            tier = "regular"
        rec = {
            "station_code": code, "station_name": name, "zone": zone or "", "state": state or "",
            "station_type": stype, "tier": tier, "terminus_trains": n_term,
            "lat": round(lat, COORD_DECIMALS), "lon": round(lon, COORD_DECIMALS),
        }
        rec["quiet_json"] = json.dumps({
            "type": "Feature", "id": code, "geometry": {"type": "Point", "coordinates": [rec["lon"], rec["lat"]]},
            "properties": {
                "station_code": code, "station_name": name, "zone": rec["zone"], "station_type": stype, "tier": tier,
                "terminus_trains": n_term, "today_activity": 0, "status": "clear",
            },
        }, separators=(",", ":"))
        out.append(rec)
    with _lock:
        _fingerprint_checked_at["station"] = time.monotonic()
        _cache["station_key"] = key
        _cache["stations"] = out
        _cache["station_index"] = {s["station_code"]: s for s in out}
    return out


def station_index(db) -> dict:
    static_stations(db)
    return _cache["station_index"]


def warm_cache(session_factory) -> None:
    """Called once at startup on a background thread so the first map load
    doesn't pay the one-off geometry parse / terminus derivation."""
    def _run():
        db = session_factory()
        try:
            static_corridors(db)
            static_stations(db)
        except Exception as e:  # warming is best-effort; the endpoints build lazily anyway
            print(f"[map] cache warm-up skipped: {e}")
        finally:
            db.close()
    threading.Thread(target=_run, name="map-cache-warm", daemon=True).start()


# ============================================================ dynamic: blocks for a date

def active_plan(db, horizon: str):
    return (
        db.query(models.BlockPlan)
        .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
        .order_by(models.BlockPlan.version.desc())
        .first()
    )


def _hhmm(value) -> str:
    return to_ist(value).strftime("%H:%M")


def day_blocks(db, date: dt.date, horizon: str, viewer) -> dict:
    """Every corridor closure touching IST `date` in the horizon's current
    plan, grouped by corridor. A closure is one corridor WINDOW (same
    corridor + slot) spanning its tasks' earliest start to latest end — the
    same definition metrics.py uses for "total corridor closures"."""
    plan = active_plan(db, horizon)
    day_start, day_end = ist_date_to_utc_bounds(date)
    result = {"plan": plan, "by_corridor": {}, "day_start": day_start, "day_end": day_end}
    if not plan:
        return result

    entries = (
        db.query(models.BlockPlanEntry)
        .filter(
            models.BlockPlanEntry.plan_id == plan.plan_id,
            models.BlockPlanEntry.assigned_window_start < day_end,
            models.BlockPlanEntry.assigned_window_end > day_start,
        )
        .all()
    )
    if not entries:
        return result
    tasks = {
        t.task_id: t for t in db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.task_id.in_([e.task_id for e in entries])).all()
    }
    gangs = crew_capacity.gang_map(db, plan.plan_id) if crew_capacity else {}

    now = utc_now()
    today = ist_today()
    viewer_dept = viewer.department if viewer is not None and viewer.role in DEPT_ROLES else None

    windows = defaultdict(list)
    for e in entries:
        windows[(e.corridor_id, e.slot_id)].append(e)

    by_corridor = defaultdict(list)
    for (corridor_id, slot_id), group in windows.items():
        group.sort(key=lambda e: e.assigned_window_start)
        start = min(e.assigned_window_start for e in group)
        end = max(e.assigned_window_end for e in group)
        if date == today:
            status = "active" if start <= now < end else ("upcoming" if start > now else "completed")
        else:
            status = "upcoming" if date > today else "completed"
        task_rows = []
        for e in group:
            t = tasks.get(e.task_id)
            visible = viewer_dept is None or e.department == viewer_dept
            row = {
                "department": e.department, "start": utc_iso(e.assigned_window_start), "end": utc_iso(e.assigned_window_end),
                "start_ist": _hhmm(e.assigned_window_start), "end_ist": _hhmm(e.assigned_window_end),
                "active_now": e.assigned_window_start <= now < e.assigned_window_end,
            }
            if visible:
                row.update({
                    "task_id": e.task_id, "defect_type": t.defect_type if t else None, "asset_id": t.asset_id if t else None,
                    "gang_id": gangs.get(e.task_id), "override": bool(e.override),
                })
            else:
                row["redacted"] = True
            task_rows.append(row)
        clipped_hours = (min(end, day_end) - max(start, day_start)).total_seconds() / 3600.0
        by_corridor[corridor_id].append({
            "slot_id": slot_id, "status": status,
            "start": utc_iso(start), "end": utc_iso(end), "start_ist": _hhmm(start), "end_ist": _hhmm(end),
            "starts_on_date_ist": to_ist(start).date().isoformat(),
            "departments": sorted({e.department for e in group}),
            "task_count": len(group), "tasks": task_rows,
            "hours_on_date": round(max(0.0, clipped_hours), 2),
            "gang_assignments": [
                f"{r['gang_id']} — {r['defect_type']} ({r['department']}), {r['start_ist']}–{r['end_ist']} IST"
                for r in task_rows if r.get("gang_id")
            ],
        })
    for blocks in by_corridor.values():
        blocks.sort(key=lambda b: b["start"])
    result["by_corridor"] = dict(by_corridor)
    return result


_STATUS_RANK = {"active": 3, "upcoming": 2, "completed": 1, "clear": 0}


def corridor_status(blocks: list) -> dict:
    if not blocks:
        return {"status": "clear", "active_block": None,
                "today_summary": {"total_blocks_today": 0, "total_hours_blocked": 0.0, "departments_involved": []}}
    status = max((b["status"] for b in blocks), key=lambda s: _STATUS_RANK[s])
    featured = None
    if status == "active":
        featured = next(b for b in blocks if b["status"] == "active")
    elif status == "upcoming":
        featured = next(b for b in blocks if b["status"] == "upcoming")
    return {
        "status": status,
        "active_block": featured,
        "today_summary": {
            "total_blocks_today": len(blocks),
            "total_hours_blocked": round(sum(b["hours_on_date"] for b in blocks), 2),
            "departments_involved": sorted({d for b in blocks for d in b["departments"]}),
        },
    }


def _fallback_geometry(db, corridor_id: str):
    """A corridor referenced by the plan but absent from the corridor master
    (e.g. an older free-text corridor id): draw it between its two endpoint
    stations if both have real coordinates; otherwise it can't be placed."""
    parts = (corridor_id or "").split("-")
    if len(parts) != 2:
        return None
    idx = station_index(db)
    a, b = idx.get(parts[0]), idx.get(parts[1])
    if not a or not b:
        return None
    return {
        "corridor_id": corridor_id, "kind": "unmastered", "from_station": parts[0], "to_station": parts[1],
        "stations": parts, "coords": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]],
        "distance_km": round(_haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]), 2), "zone": a["zone"],
        "train_count": 0, "section_count": 1, "geometry_source": "endpoint_stations",
    }


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _in_bbox(coords, bbox):
    if not bbox:
        return True
    min_lon, min_lat, max_lon, max_lat = bbox
    return any(min_lon <= x <= max_lon and min_lat <= y <= max_lat for x, y in coords)


def _feature_collection_json(feature_strings: list, meta: dict) -> str:
    return '{"type":"FeatureCollection","features":[' + ",".join(feature_strings) + '],"meta":' + json.dumps(meta, separators=(",", ":")) + "}"


def corridors_geojson(db, date: dt.date, horizon: str, viewer, include_clear: bool = True, bbox=None) -> str:
    """Returns the serialized FeatureCollection (a str, not a dict): clear
    corridors are spliced in from their cached fragments; only corridors
    with blocks on `date` are serialized per request."""
    t0 = time.perf_counter()
    corridors = static_corridors(db)
    blocks = day_blocks(db, date, horizon, viewer)
    by_corridor = blocks["by_corridor"]
    index = corridor_index(db)

    extra, unmapped = [], []
    for cid in by_corridor:
        if cid not in index:
            fb = _fallback_geometry(db, cid)
            if fb:
                extra.append(fb)
            else:
                unmapped.append(cid)

    features = []
    counts = defaultdict(int)
    for c in (corridors + extra) if extra else corridors:
        corridor_blocks = by_corridor.get(c["corridor_id"])
        if not corridor_blocks and not include_clear:
            continue
        if bbox and not _in_bbox(c["coords"], bbox):
            continue
        if not corridor_blocks:
            features.append(c["clear_json"])
            counts["clear"] += 1
            continue
        props = {
            "corridor_id": c["corridor_id"], "kind": c["kind"], "from_station": c["from_station"],
            "to_station": c["to_station"], "distance_km": c["distance_km"], "zone": c["zone"],
            **corridor_status(corridor_blocks),
            "blocks_today": corridor_blocks, "train_count": c["train_count"], "stations": c["stations"],
            "geometry_source": c["geometry_source"],
        }
        counts[props["status"]] += 1
        features.append(json.dumps({
            "type": "Feature", "id": c["corridor_id"],
            "geometry": {"type": "LineString", "coordinates": c["coords"]},
            "properties": props,
        }, separators=(",", ":")))

    plan = blocks["plan"]
    meta = {
        "date_ist": date.isoformat(), "horizon": horizon, "generated_at": utc_iso(utc_now()),
        "plan_id": plan.plan_id if plan else None, "plan_status": plan.status if plan else None,
        "corridors_with_blocks": len(by_corridor), "corridors_unmappable": unmapped,
        "feature_count": len(features), "status_counts": dict(counts), "include_clear": include_clear,
        "gang_assignments_available": crew_capacity is not None,
        "build_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    return _feature_collection_json(features, meta)


def _station_activity(db, by_corridor: dict) -> dict:
    """station_code -> {closures, departments today, departments active now}."""
    index = corridor_index(db)
    activity = {}
    for cid, blocks in by_corridor.items():
        rec = index.get(cid)
        codes = rec["stations"] if rec else cid.split("-")
        for code in codes:
            a = activity.setdefault(code, {"blocks": 0, "status": "clear", "dept_tasks": defaultdict(int), "active_dept_tasks": defaultdict(int)})
            for b in blocks:
                a["blocks"] += 1
                if _STATUS_RANK[b["status"]] > _STATUS_RANK[a["status"]]:
                    a["status"] = b["status"]
                for t in b["tasks"]:
                    a["dept_tasks"][t["department"]] += 1
                    if b["status"] == "active":
                        a["active_dept_tasks"][t["department"]] += 1
    return activity


def _dominant(counts: dict):
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def stations_geojson(db, date: dt.date, horizon: str, viewer, bbox=None, include_quiet: bool = True) -> str:
    """Serialized FeatureCollection (str) — quiet stations spliced from cached
    fragments, only stations touched by a block on `date` built per request."""
    t0 = time.perf_counter()
    stations = static_stations(db)
    blocks = day_blocks(db, date, horizon, viewer)
    activity = _station_activity(db, blocks["by_corridor"])
    features = []
    tier_counts = defaultdict(int)
    for s in stations:
        tier_counts[s["tier"]] += 1
        if bbox:
            min_lon, min_lat, max_lon, max_lat = bbox
            if not (min_lon <= s["lon"] <= max_lon and min_lat <= s["lat"] <= max_lat):
                continue
        a = activity.get(s["station_code"])
        if not a:
            if include_quiet:
                features.append(s["quiet_json"])
            continue
        props = {
            "station_code": s["station_code"], "station_name": s["station_name"], "zone": s["zone"],
            "station_type": s["station_type"], "tier": s["tier"], "terminus_trains": s["terminus_trains"],
            "today_activity": a["blocks"], "status": a["status"],
            "departments_today": sorted(a["dept_tasks"]),
            "active_departments": sorted(a["active_dept_tasks"]),
            "dominant_department": _dominant(a["active_dept_tasks"]) or _dominant(a["dept_tasks"]),
        }
        features.append(json.dumps({
            "type": "Feature", "id": s["station_code"],
            "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
            "properties": props,
        }, separators=(",", ":")))
    plan = blocks["plan"]
    with _lock:
        total_master = _cache.get("station_master_total")
    if total_master is None or not _fresh_enough("station"):
        total_master = db.query(func.count(models.Station.station_code)).scalar()
        with _lock:
            _cache["station_master_total"] = total_master
    meta = {
        "date_ist": date.isoformat(), "horizon": horizon, "generated_at": utc_iso(utc_now()),
        "plan_id": plan.plan_id if plan else None, "feature_count": len(features), "include_quiet": include_quiet,
        "stations_in_master": total_master, "stations_with_coordinates": len(stations),
        "stations_without_coordinates": total_master - len(stations),
        "stations_with_activity": sum(1 for code in activity if code in station_index(db)),
        "tier_counts": dict(tier_counts),
        "major_tier_rule": f"station_type junction/terminal, or >= {MAJOR_TERMINUS_MIN_TRAINS} real trains start/end there",
        "build_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    return _feature_collection_json(features, meta)


# ============================================================ detail popups

def _summarize_hourly(rows, source: str, fetched_at=None) -> dict:
    precip = [r["precipitation_probability_pct"] for r in rows if r.get("precipitation_probability_pct") is not None]
    wind = [r["wind_speed_kmh"] for r in rows if r.get("wind_speed_kmh") is not None]
    vis = [r["visibility_km"] for r in rows if r.get("visibility_km") is not None]
    temp = [r["temperature_c"] for r in rows if r.get("temperature_c") is not None]
    lightning_hours = [_hhmm(r["valid_time"]) for r in rows if r.get("lightning_risk")]
    fog_hours = [_hhmm(r["valid_time"]) for r in rows if r.get("fog_risk")]
    now = utc_now()
    current = min(rows, key=lambda r: abs((r["valid_time"] - now).total_seconds())) if rows else None
    return {
        "available": True, "source": source, "hours": len(rows),
        "fetched_at": utc_iso(fetched_at) if fetched_at else None,
        "max_precipitation_probability_pct": max(precip) if precip else None,
        "max_wind_speed_kmh": round(max(wind), 1) if wind else None,
        "min_visibility_km": round(min(vis), 1) if vis else None,
        "temperature_min_c": round(min(temp), 1) if temp else None,
        "temperature_max_c": round(max(temp), 1) if temp else None,
        "lightning_hours_ist": lightning_hours, "fog_hours_ist": fog_hours,
        "nearest_hour": {
            "valid_ist": _hhmm(current["valid_time"]), "temperature_c": current.get("temperature_c"),
            "precipitation_probability_pct": current.get("precipitation_probability_pct"),
            "wind_speed_kmh": current.get("wind_speed_kmh"), "weather_code": current.get("weather_code"),
        } if current else None,
    }


def _ingested_weather(db, corridor_ids: list, date: dt.date):
    day_start, day_end = ist_date_to_utc_bounds(date)
    for cid in corridor_ids:
        rows = (
            db.query(models.WeatherHourlyForecast)
            .filter(models.WeatherHourlyForecast.corridor_id == cid,
                    models.WeatherHourlyForecast.valid_time >= day_start,
                    models.WeatherHourlyForecast.valid_time < day_end)
            .order_by(models.WeatherHourlyForecast.valid_time).all()
        )
        if rows:
            dicts = [{
                "valid_time": r.valid_time, "temperature_c": r.temperature_c,
                "precipitation_probability_pct": r.precipitation_probability_pct, "wind_speed_kmh": r.wind_speed_kmh,
                "visibility_km": r.visibility_km, "weather_code": r.weather_code,
                "lightning_risk": r.lightning_risk, "fog_risk": r.fog_risk,
            } for r in rows]
            summary = _summarize_hourly(dicts, f"open_meteo (ingested for {cid} — what the optimizer uses)", rows[0].fetched_at)
            summary["corridor_id"] = cid
            return summary
    return None


def _live_weather(lat: float, lon: float, date: dt.date):
    """Read-only live Open-Meteo lookup for a map popup when nothing has been
    ingested for this place. Never written to the DB (so it can never
    silently feed the optimizer) and cached for 30 min per location/date.
    Returns available=False with the real error when the provider is
    unreachable — no values are ever invented."""
    key = (round(lat, 2), round(lon, 2), date)
    hit = _weather_cache.get(key)
    if hit and time.monotonic() - hit[0] < LIVE_WEATHER_TTL_SECONDS:
        return hit[1]
    days_ahead = (date - ist_today()).days
    if days_ahead < 0 or days_ahead > 15:
        return {"available": False, "reason": "live forecast only covers today through the next 15 days"}
    try:
        from adapters.weather import WEATHER_ADAPTERS
        rows = WEATHER_ADAPTERS["open_meteo"].fetch_forecast(lat, lon, days=days_ahead + 1)
    except Exception as e:
        return {"available": False, "reason": f"weather provider unreachable — nothing fabricated ({e})"}
    day_start, day_end = ist_date_to_utc_bounds(date)
    day_rows = [r for r in rows if day_start <= r["valid_time"] < day_end]
    if not day_rows:
        summary = {"available": False, "reason": "provider returned no hours for this date"}
    else:
        summary = _summarize_hourly(day_rows, "open_meteo (live preview for this map popup, not ingested)", utc_now())
        summary["location"] = {"lat": lat, "lon": lon}
    _weather_cache[key] = (time.monotonic(), summary)
    return summary


def corridor_detail(db, corridor_id: str, date: dt.date, horizon: str, viewer, live_weather: bool = True) -> dict:
    rec = corridor_index(db).get(corridor_id) or _fallback_geometry(db, corridor_id)
    if rec is None:
        raise ValueError(f"corridor '{corridor_id}' has no mappable geometry (not in the corridor master and its endpoints lack coordinates)")
    blocks = day_blocks(db, date, horizon, viewer)["by_corridor"].get(corridor_id, [])
    idx = station_index(db)
    names = {code: (idx[code]["station_name"] if code in idx else None) for code in rec["stations"]}

    weather = _ingested_weather(db, [corridor_id], date)
    if weather is None and live_weather:
        mid = rec["coords"][len(rec["coords"]) // 2]
        weather = _live_weather(mid[1], mid[0], date)
    if weather is None:
        weather = {"available": False, "reason": "no forecast ingested for this corridor"}

    return {
        "corridor_id": corridor_id, "kind": rec["kind"], "date_ist": date.isoformat(),
        "from_station": rec["from_station"], "to_station": rec["to_station"],
        "from_station_name": names.get(rec["from_station"]), "to_station_name": names.get(rec["to_station"]),
        "stations": [{"station_code": c, "station_name": names.get(c)} for c in rec["stations"]],
        "distance_km": rec["distance_km"], "zone": rec["zone"], "train_count": rec["train_count"],
        "geometry": {"type": "LineString", "coordinates": rec["coords"]},
        "geometry_source": rec["geometry_source"],
        **corridor_status(blocks),
        "blocks_today": blocks,
        "weather": weather,
    }


def station_detail(db, station_code: str, date: dt.date, horizon: str, viewer, live_weather: bool = True) -> dict:
    requested = station_code.upper()
    code = STATION_CODE_ALIASES.get(requested, requested)
    st = db.query(models.Station).filter_by(station_code=code).first()
    if not st:
        raise ValueError(f"station '{requested}' not found in the station master")

    # Trains: the timetable stores daily-recurring segment templates whose
    # time-of-day is IST (see timetable_loader.corridor_occurrences).
    deps = db.query(models.TrainTimetableEntry).filter(models.TrainTimetableEntry.from_station_code == code).all()
    arrs = db.query(models.TrainTimetableEntry.train_id).filter(models.TrainTimetableEntry.to_station_code == code).distinct().all()
    now_ist = to_ist(utc_now())
    departures = {}
    for d in deps:
        dep_t = to_ist(d.scheduled_departure).time()
        if d.train_id not in departures or dep_t < departures[d.train_id]["_t"]:
            departures[d.train_id] = {
                "_t": dep_t, "train_id": d.train_id, "train_name": d.train_name,
                "departure_ist": dep_t.strftime("%H:%M"), "next_station": d.to_station_code, "corridor_id": d.corridor_id,
            }
    train_ids = set(departures) | {a[0] for a in arrs}
    ordered = sorted(departures.values(), key=lambda r: r["_t"])
    if date == now_ist.date():
        upcoming = [r for r in ordered if r["_t"] >= now_ist.time().replace(second=0, microsecond=0)]
        basis = f"next departures after {now_ist.strftime('%H:%M')} IST"
    else:
        upcoming = ordered
        basis = f"first departures on {date.isoformat()}"
    next_three = [{k: v for k, v in r.items() if k != "_t"} for r in upcoming[:3]]

    by_corridor = day_blocks(db, date, horizon, viewer)["by_corridor"]
    touching = corridors_by_station(db).get(code, [])
    touching_set = set(touching) | {cid for cid in by_corridor if code in cid.split("-")}
    blocks = []
    for cid in sorted(touching_set):
        for b in by_corridor.get(cid, []):
            blocks.append({"corridor_id": cid, **b})
    blocks.sort(key=lambda b: (-_STATUS_RANK[b["status"]], b["start"]))

    weather = _ingested_weather(db, [c for c in touching if c in by_corridor] + list(touching)[:25], date)
    if weather is None and live_weather and st.lat is not None:
        weather = _live_weather(st.lat, st.lon, date)
    if weather is None:
        weather = {"available": False, "reason": "no coordinates / forecast for this station"}

    return {
        "station_code": code, "requested_code": requested, "alias_applied": requested != code,
        "station_name": st.station_name, "zone": st.zone, "state": st.state, "station_type": st.station_type,
        "lat": st.lat, "lon": st.lon, "date_ist": date.isoformat(),
        "trains_today": {
            "count": len(train_ids), "departing_count": len(departures), "next_departures": next_three,
            "basis": basis, "note": "timetable segments are daily-recurring templates (no day-of-week running data in the open dataset)",
        },
        "blocks": blocks,
        "weather": weather,
    }
