"""Corridor vacancy from REAL train movements (Fix 3).

Nothing here is random or manually seeded: a vacancy window is a period in
which the loaded, real timetable shows NO train occupying ANY section of
the corridor, with a safety buffer applied around every movement.

ALGORITHM (exactly the operational definition, not an approximation):

  1. Collect every scheduled movement through each SECTION of the corridor
     from the real timetable, expanded onto the requested IST calendar
     dates (timetable_loader.corridor_occurrences already does this
     IST-correctly — a train published at 00:15 IST belongs to that IST
     day, not the UTC one).

  2. Occupy each section from the train's departure at station A to its
     arrival at station B, extended by BUFFER_MINUTES on BOTH sides
     (default 15) — signal clearance and safety margin. Overlapping
     occupations are merged.

  3. Gaps are the periods with no occupation. For a MULTI-SECTION corridor
     a window only counts if EVERY section is simultaneously free, so the
     per-section gap sets are INTERSECTED — a corridor is only vacant when
     all of it is vacant.

  4. Windows shorter than MIN_WINDOW_HOURS (default 1.0) are discarded: a
     ten-minute gap is operationally useless for a maintenance block.

  5. Each surviving window records the REAL trains bracketing it
     (train_before_id / train_after_id) so any user can verify the window
     against the published timetable rather than taking it on trust.

The static timetable is the ONLY input — this computation is fully offline
and never depends on a live API being reachable. Live running data (delays)
is an optional enrichment layer elsewhere; vacancy never depends on it.
"""
import datetime as dt
import uuid
from typing import List, Tuple

from sqlalchemy.orm import Session

import corridor_builder
import models
import timetable_loader
from tz_utils import IST, ist_date_to_utc_bounds, to_ist

DEFAULT_BUFFER_MINUTES = 15
DEFAULT_MIN_WINDOW_HOURS = 1.0
MAX_DATE_RANGE_DAYS = 60


def _merge_occupations(spans: List[Tuple[dt.datetime, dt.datetime, str]]):
    """Merges overlapping/touching occupied spans, keeping the train id that
    ENDS each merged block (the train you wait for) and the one that STARTS
    it (the train that just cleared)."""
    if not spans:
        return []
    spans = sorted(spans, key=lambda s: s[0])
    merged = [{"start": spans[0][0], "end": spans[0][1], "first_train": spans[0][2], "last_train": spans[0][2]}]
    for start, end, train in spans[1:]:
        block = merged[-1]
        if start <= block["end"]:
            if end > block["end"]:
                block["end"] = end
                block["last_train"] = train
        else:
            merged.append({"start": start, "end": end, "first_train": train, "last_train": train})
    return merged


def _section_free_intervals(occupied, window_start, window_end):
    """Complement of the occupied blocks within [window_start, window_end],
    each carrying the bracketing train ids."""
    free = []
    cursor = window_start
    prev_train = None
    for block in occupied:
        if block["start"] > cursor:
            free.append({"start": cursor, "end": block["start"], "before": prev_train, "after": block["first_train"]})
        cursor = max(cursor, block["end"])
        prev_train = block["last_train"]
    if cursor < window_end:
        free.append({"start": cursor, "end": window_end, "before": prev_train, "after": None})
    return free


def _intersect(a_list, b_list):
    """Intersect two sets of free intervals. A corridor is vacant only where
    ALL of its sections are simultaneously vacant, so this is applied
    pairwise across every section."""
    out = []
    i = j = 0
    while i < len(a_list) and j < len(b_list):
        a, b = a_list[i], b_list[j]
        start = max(a["start"], b["start"])
        end = min(a["end"], b["end"])
        if start < end:
            # Keep the most constraining bracketing trains: the one that
            # cleared latest before the window, and the one arriving first
            # after it.
            before = a["before"] if a["start"] >= b["start"] else b["before"]
            after = a["after"] if a["end"] <= b["end"] else b["after"]
            out.append({"start": start, "end": end, "before": before, "after": after})
        if a["end"] < b["end"]:
            i += 1
        else:
            j += 1
    return out


def compute_vacancy(
    db: Session,
    corridor_id: str,
    date_from: dt.date,
    date_to: dt.date,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
    min_window_hours: float = DEFAULT_MIN_WINDOW_HOURS,
) -> dict:
    """Returns computed windows (not persisted) plus the inputs used, so a
    caller can show its work. date_from/date_to are IST calendar dates."""
    if date_to < date_from:
        raise ValueError("date_to must not be before date_from")
    span_days = (date_to - date_from).days + 1
    if span_days > MAX_DATE_RANGE_DAYS:
        raise ValueError(f"date range too large ({span_days} days); maximum is {MAX_DATE_RANGE_DAYS}")

    section_ids = corridor_builder.get_section_ids(db, corridor_id)
    horizon_begin, _ = ist_date_to_utc_bounds(date_from)
    horizon_end = horizon_begin + dt.timedelta(days=span_days)
    buffer = dt.timedelta(minutes=buffer_minutes)

    per_section_free = []
    movements_considered = 0
    sections_with_data = 0
    for sec in section_ids:
        occurrences = timetable_loader.corridor_occurrences(db, sec, date_from, span_days)
        movements_considered += len(occurrences)
        if occurrences:
            sections_with_data += 1
        spans = [
            (occ["start"] - buffer, occ["end"] + buffer, occ["train_id"])
            for occ in occurrences
        ]
        merged = _merge_occupations(spans)
        per_section_free.append(_section_free_intervals(merged, horizon_begin, horizon_end))

    if not per_section_free:
        free = []
    else:
        free = per_section_free[0]
        for other in per_section_free[1:]:
            free = _intersect(free, other)

    min_delta = dt.timedelta(hours=min_window_hours)
    windows = []
    for iv in free:
        duration = iv["end"] - iv["start"]
        if duration < min_delta:
            continue
        windows.append(
            {
                "start": iv["start"],
                "end": iv["end"],
                "duration_hours": round(duration.total_seconds() / 3600.0, 2),
                "train_before_id": iv["before"],
                "train_after_id": iv["after"],
            }
        )

    return {
        "corridor_id": corridor_id,
        "section_ids": section_ids,
        "section_count": len(section_ids),
        "sections_with_timetable_data": sections_with_data,
        "date_from_ist": date_from.isoformat(),
        "date_to_ist": date_to.isoformat(),
        "buffer_minutes": buffer_minutes,
        "min_window_hours": min_window_hours,
        "real_train_movements_considered": movements_considered,
        "windows_found": len(windows),
        "total_available_hours": round(sum(w["duration_hours"] for w in windows), 2),
        "windows": windows,
    }


def derive_and_store(
    db: Session,
    corridor_id: str,
    date_from: dt.date,
    date_to: dt.date,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
    min_window_hours: float = DEFAULT_MIN_WINDOW_HOURS,
    horizon: str = "weekly",
) -> dict:
    """Computes vacancy and persists it as CorridorSlot rows. Re-runnable:
    previously DERIVED slots for this corridor in this date range are
    replaced, never duplicated. Manually-entered slots
    (derived_from='manual') are deliberately left untouched — the Control
    Office's own entries are not the algorithm's to delete."""
    result = compute_vacancy(db, corridor_id, date_from, date_to, buffer_minutes, min_window_hours)

    range_start, _ = ist_date_to_utc_bounds(date_from)
    range_end = range_start + dt.timedelta(days=(date_to - date_from).days + 1)
    db.query(models.CorridorSlot).filter(
        models.CorridorSlot.corridor_id == corridor_id,
        models.CorridorSlot.derived_from == "timetable_gap",
        models.CorridorSlot.start_time >= range_start,
        models.CorridorSlot.start_time < range_end,
    ).delete(synchronize_session=False)

    created = []
    for w in result["windows"]:
        slot = models.CorridorSlot(
            slot_id=f"slot-{uuid.uuid4().hex[:8]}",
            corridor_id=corridor_id,
            start_time=w["start"],
            end_time=w["end"],
            status="available",
            derived_from="timetable_gap",
            horizon=horizon,
            train_before_id=w["train_before_id"] or "",
            train_after_id=w["train_after_id"] or "",
            buffer_minutes=buffer_minutes,
            sections_considered=result["section_count"],
        )
        db.add(slot)
        created.append(
            {
                "slot_id": slot.slot_id,
                "start_time": w["start"],
                "end_time": w["end"],
                "start_ist": to_ist(w["start"]).strftime("%Y-%m-%d %H:%M"),
                "end_ist": to_ist(w["end"]).strftime("%Y-%m-%d %H:%M"),
                "duration_hours": w["duration_hours"],
                "train_before_id": w["train_before_id"],
                "train_after_id": w["train_after_id"],
            }
        )
    db.commit()

    result["slots_created"] = created
    result.pop("windows", None)
    return result


def verify_no_overlap(db: Session, corridor_id: str, date_from: dt.date, date_to: dt.date, buffer_minutes: int = DEFAULT_BUFFER_MINUTES) -> dict:
    """Independent correctness check (verification #6): re-expands every
    real movement on every section of the corridor and asserts that NO
    stored vacancy window overlaps any of them, buffer included. Deliberately
    written as a separate, naive O(n*m) scan rather than reusing the
    computation above — a check that shares the code it is checking proves
    nothing."""
    section_ids = corridor_builder.get_section_ids(db, corridor_id)
    span_days = (date_to - date_from).days + 1
    buffer = dt.timedelta(minutes=buffer_minutes)

    occupied = []
    for sec in section_ids:
        for occ in timetable_loader.corridor_occurrences(db, sec, date_from, span_days):
            occupied.append((occ["start"] - buffer, occ["end"] + buffer, occ["train_id"], sec))

    range_start, _ = ist_date_to_utc_bounds(date_from)
    range_end = range_start + dt.timedelta(days=span_days)
    slots = (
        db.query(models.CorridorSlot)
        .filter(
            models.CorridorSlot.corridor_id == corridor_id,
            models.CorridorSlot.derived_from == "timetable_gap",
            models.CorridorSlot.start_time >= range_start,
            models.CorridorSlot.start_time < range_end,
        )
        .all()
    )

    violations = []
    for s in slots:
        for occ_start, occ_end, train_id, sec in occupied:
            if occ_start < s.end_time and s.start_time < occ_end:
                violations.append(
                    {
                        "slot_id": s.slot_id,
                        "slot_start_ist": to_ist(s.start_time).strftime("%Y-%m-%d %H:%M"),
                        "slot_end_ist": to_ist(s.end_time).strftime("%Y-%m-%d %H:%M"),
                        "train_id": train_id,
                        "section": sec,
                        "train_start_ist": to_ist(occ_start).strftime("%Y-%m-%d %H:%M"),
                        "train_end_ist": to_ist(occ_end).strftime("%Y-%m-%d %H:%M"),
                    }
                )
    return {
        "corridor_id": corridor_id,
        "sections_checked": len(section_ids),
        "windows_checked": len(slots),
        "train_movements_checked": len(occupied),
        "buffer_minutes": buffer_minutes,
        "violations": violations,
        "clean": not violations,
    }
