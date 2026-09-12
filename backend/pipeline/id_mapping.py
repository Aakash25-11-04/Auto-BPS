"""Asset & Corridor ID Mapping.

TMS, SMMS, and TDMS are three different real systems and, in reality, each
has its own asset-numbering convention (a bare chainage code, a prefixed
asset tag, an internal serial, etc.). Rather than assume they already agree
with ABPS's canonical IDs, every incoming row's asset_id and corridor_id are
resolved through explicit mapping tables before anything is inserted as a
MaintenanceTask.

Resolution order for an asset_id:
  1. An explicit mapping row for (source_system, source_asset_id) exists ->
     use its canonical_asset_id.
  2. No mapping exists, but the incoming ID already matches a canonical
     asset_id registered in AssetCriticality (this is the common case for
     manual entry, where an operator already types the canonical ID
     directly) -> treat it as already-canonical (identity resolution).
  3. Neither -> UNRESOLVED. The row is never guessed at or silently
     dropped; it goes to the review queue for a human to either register a
     new mapping or correct the source data.

Corridor IDs use the same two-step resolution, but with a more permissive
fallback: a corridor_id that doesn't match any mapping AND isn't a known
canonical corridor is still accepted as freeform (ABPS does not maintain a
closed corridor master list the way it does for assets — a corridor is
whatever real timetable segment it turns out to be) UNLESS the caller asks
for strict resolution, which the TMS/SMMS/TDMS pipeline path does.
"""
from sqlalchemy.orm import Session

import models


def resolve_asset_id(db: Session, source_system: str, raw_asset_id: str) -> str | None:
    mapping = (
        db.query(models.AssetIdMapping)
        .filter_by(source_system=source_system, source_asset_id=raw_asset_id)
        .first()
    )
    if mapping:
        return mapping.canonical_asset_id

    if db.query(models.AssetCriticality).filter_by(asset_id=raw_asset_id).first():
        return raw_asset_id

    return None


def resolve_corridor_id(db: Session, source_system: str, raw_corridor_id: str, strict: bool = False) -> str | None:
    mapping = (
        db.query(models.CorridorIdMapping)
        .filter_by(source_system=source_system, source_corridor_id=raw_corridor_id)
        .first()
    )
    if mapping:
        return mapping.canonical_corridor_id

    if not strict:
        return raw_corridor_id  # freeform passthrough — see module docstring

    is_known = (
        db.query(models.TrainTimetableEntry).filter_by(corridor_id=raw_corridor_id).first()
        or db.query(models.CorridorSlot).filter_by(corridor_id=raw_corridor_id).first()
    )
    return raw_corridor_id if is_known else None


def register_asset_mapping(db: Session, source_system: str, source_asset_id: str, canonical_asset_id: str):
    existing = (
        db.query(models.AssetIdMapping)
        .filter_by(source_system=source_system, source_asset_id=source_asset_id)
        .first()
    )
    if existing:
        existing.canonical_asset_id = canonical_asset_id
    else:
        db.add(
            models.AssetIdMapping(
                source_system=source_system, source_asset_id=source_asset_id, canonical_asset_id=canonical_asset_id
            )
        )
    db.commit()


def register_corridor_mapping(db: Session, source_system: str, source_corridor_id: str, canonical_corridor_id: str):
    existing = (
        db.query(models.CorridorIdMapping)
        .filter_by(source_system=source_system, source_corridor_id=source_corridor_id)
        .first()
    )
    if existing:
        existing.canonical_corridor_id = canonical_corridor_id
    else:
        db.add(
            models.CorridorIdMapping(
                source_system=source_system,
                source_corridor_id=source_corridor_id,
                canonical_corridor_id=canonical_corridor_id,
            )
        )
    db.commit()
