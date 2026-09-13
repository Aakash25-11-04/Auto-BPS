"""Real corridor derivation (Fix 2) — corridors from actual railway
geography, never invented.

HOW A CORRIDOR IS DERIVED (nothing here is synthetic):

  1. Every consecutive pair of stops for a real train in the loaded
     timetable is a physical track SECTION (e.g. NDLS->GZB). Sections are
     observed facts about where trains actually run — this is the same
     definition timetable_loader.py already uses for TrainTimetableEntry.

  2. A ROUTE corridor is a contiguous CHAIN of those sections running
     between two SIGNIFICANT stations — junctions or terminals, per the
     station master's station_type (see station_loader.classify_station).
     That is how railway staff actually delimit a stretch of line: from one
     junction, through the smaller stations in between, to the next
     junction.

  3. Chains are walked over the real section graph. Starting at each
     significant station, the walk follows sections through NON-significant
     stations only, and stops the moment it reaches another significant
     station (or the line branches, i.e. a station has more than one
     onward section — a branch point is a real junction in practice even
     if its name doesn't say "JN").

  4. Geometry, distance and intermediate stations come from the REAL
     station master coordinates (haversine between consecutive stops), so a
     corridor's polyline is genuine railway geography, not a straight line
     between two endpoints.

Both kinds are stored in the same `corridors` table: every section is
itself a corridor (kind='section', one section long), and routes are
kind='route'. This keeps every corridor_id already referenced by tasks,
slots and plans valid, while adding the richer junction-to-junction
corridors on top.
"""
import json
import math
from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

import models

SIGNIFICANT_TYPES = {"junction", "terminal"}
MAX_ROUTE_SECTIONS = 40  # a safety bound so a pathological chain can't run away


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _path_geometry_and_distance(stations: list, station_by_code: dict):
    """GeoJSON LineString + total distance along the REAL station
    coordinates of every stop in order. Returns (geojson_str, km or None)."""
    coords = []
    total = 0.0
    prev = None
    for code in stations:
        s = station_by_code.get(code)
        if not s or s.lat is None or s.lon is None:
            continue
        coords.append([round(s.lon, 6), round(s.lat, 6)])
        if prev is not None:
            total += _haversine_km(prev.lat, prev.lon, s.lat, s.lon)
        prev = s
    if len(coords) < 2:
        return "", None
    return json.dumps({"type": "LineString", "coordinates": coords}), round(total, 2)


def build_corridors(db: Session, min_trains_for_route: int = 1) -> dict:
    """Rebuilds the corridor master from the loaded real timetable. Fully
    re-runnable: corridors are upserted by corridor_id, so re-running after
    a timetable reload refreshes counts/geometry without duplicating."""
    sections = (
        db.query(
            models.TrainTimetableEntry.corridor_id,
            models.TrainTimetableEntry.from_station_code,
            models.TrainTimetableEntry.to_station_code,
        )
        .distinct()
        .all()
    )
    if not sections:
        raise ValueError("no timetable entries loaded — run POST /api/data/load-timetable first")

    train_counts = dict(
        db.query(
            models.TrainTimetableEntry.corridor_id,
            func.count(func.distinct(models.TrainTimetableEntry.train_id)),
        )
        .group_by(models.TrainTimetableEntry.corridor_id)
        .all()
    )

    station_by_code = {s.station_code: s for s in db.query(models.Station).all()}
    existing = {c.corridor_id: c for c in db.query(models.Corridor).all()}

    def significant(code: str) -> bool:
        s = station_by_code.get(code)
        return bool(s and s.station_type in SIGNIFICANT_TYPES)

    # --- 1. every real section is a corridor in its own right -------------
    onward = defaultdict(list)  # from_code -> [(to_code, corridor_id)]
    indegree = defaultdict(int)
    section_created = 0
    for corridor_id, a, b in sections:
        onward[a].append((b, corridor_id))
        indegree[b] += 1
        row = existing.get(corridor_id)
        geom, dist = _path_geometry_and_distance([a, b], station_by_code)
        zone = (station_by_code.get(a).zone if station_by_code.get(a) else "") or ""
        if row is None:
            row = models.Corridor(corridor_id=corridor_id, kind="section", from_station_code=a, to_station_code=b)
            db.add(row)
            existing[corridor_id] = row
            section_created += 1
        row.kind = "section"
        row.from_station_code = a
        row.to_station_code = b
        row.intermediate_stations = "[]"
        row.section_ids = json.dumps([corridor_id])
        row.section_count = 1
        row.zone = zone
        row.total_distance_km = dist
        row.geometry_json = geom
        row.train_count = int(train_counts.get(corridor_id, 0))
        row.derived_from = "timetable_section"

    # --- 2. reduce to PRIMARY physical adjacency --------------------------
    # A timetable contains both stopping and express services, so the raw
    # section set mixes atomic hops (NDLS->GZB) with skips over them
    # (NDLS->CNB, an express that passes GZB without stopping). Chaining
    # over the raw set would make almost every station look like a branch
    # point and collapse every corridor to two sections.
    #
    # A section A->B is PRIMARY (a real physical hop) when no station C
    # exists with both A->C and C->B also sections — i.e. the hop cannot be
    # decomposed into smaller observed hops. Express skips are exactly the
    # decomposable ones, so this filter recovers the underlying line from
    # the timetable alone, with no external track dataset needed.
    onward_sets = {a: {b for b, _ in pairs} for a, pairs in onward.items()}
    inward_sets = defaultdict(set)
    for a, pairs in onward.items():
        for b, _ in pairs:
            inward_sets[b].add(a)

    primary_onward = defaultdict(list)  # from -> [(to, section_id)]
    primary_indegree = defaultdict(int)
    for a, pairs in onward.items():
        for b, sec in pairs:
            intermediates = onward_sets.get(a, set()) & inward_sets.get(b, set())
            intermediates.discard(a)
            intermediates.discard(b)
            if intermediates:
                continue  # decomposable => an express skip, not a physical hop
            primary_onward[a].append((b, sec))
            primary_indegree[b] += 1

    # --- 3. walk chains of PRIMARY sections between significant stations ---
    # A station ends a corridor if it is a junction/terminal by name, or if
    # the physical line genuinely branches there (more than one primary
    # section out or in) — a real branch point delimits a corridor whatever
    # its name says.
    def is_endpoint(code: str) -> bool:
        return significant(code) or len(primary_onward.get(code, [])) > 1 or primary_indegree.get(code, 0) > 1

    route_created = 0
    routes_seen = set()
    for start in list(primary_onward.keys()):
        if not is_endpoint(start):
            continue
        for first_to, first_sec in primary_onward[start]:
            chain_stations = [start, first_to]
            chain_sections = [first_sec]
            cursor = first_to
            while (
                not is_endpoint(cursor)
                and len(chain_sections) < MAX_ROUTE_SECTIONS
                and len(primary_onward.get(cursor, [])) == 1
            ):
                next_to, next_sec = primary_onward[cursor][0]
                if next_to in chain_stations:  # never loop back on ourselves
                    break
                chain_stations.append(next_to)
                chain_sections.append(next_sec)
                cursor = next_to

            if len(chain_sections) < 2:
                continue  # a single section is already stored above as kind='section'

            route_id = f"{chain_stations[0]}-{chain_stations[-1]}"
            if route_id in routes_seen:
                continue
            routes_seen.add(route_id)

            trains_on_route = min(int(train_counts.get(sec, 0)) for sec in chain_sections)
            if trains_on_route < min_trains_for_route:
                continue

            geom, dist = _path_geometry_and_distance(chain_stations, station_by_code)
            row = existing.get(route_id)
            if row is None:
                row = models.Corridor(
                    corridor_id=route_id, kind="route",
                    from_station_code=chain_stations[0], to_station_code=chain_stations[-1],
                )
                db.add(row)
                existing[route_id] = row
                route_created += 1
            elif row.kind == "section":
                # A direct section between the same two stations already
                # exists (trains run both the through-section and the
                # stopping chain). The longer chain is the more useful
                # description, so it wins — but only when it genuinely has
                # more sections, never silently.
                if row.section_count >= len(chain_sections):
                    continue
            row.kind = "route"
            row.from_station_code = chain_stations[0]
            row.to_station_code = chain_stations[-1]
            row.intermediate_stations = json.dumps(chain_stations[1:-1])
            row.section_ids = json.dumps(chain_sections)
            row.section_count = len(chain_sections)
            row.zone = (station_by_code.get(chain_stations[0]).zone if station_by_code.get(chain_stations[0]) else "") or ""
            row.total_distance_km = dist
            row.geometry_json = geom
            row.train_count = trains_on_route
            row.derived_from = "timetable_chain_between_significant_stations"

    db.commit()

    total = db.query(models.Corridor).count()
    routes = db.query(models.Corridor).filter_by(kind="route").count()
    return {
        "sections_total": total - routes,
        "routes_total": routes,
        "corridors_total": total,
        "sections_created_this_run": section_created,
        "routes_created_this_run": route_created,
    }


def get_section_ids(db: Session, corridor_id: str) -> list:
    """The ordered list of physical sections a corridor_id covers. A
    section corridor returns just itself, so callers (vacancy computation)
    never need to care which kind they were handed."""
    row = db.query(models.Corridor).filter_by(corridor_id=corridor_id).first()
    if row and row.section_ids:
        try:
            ids = json.loads(row.section_ids)
            if ids:
                return ids
        except (ValueError, TypeError):
            pass
    return [corridor_id]
