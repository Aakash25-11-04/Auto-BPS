"""Layer 2 visibility: ingestion batch stats, the review queue, and
asset/corridor ID mapping management.

RBAC: viewing pipeline stats/review-queue items is open to any authenticated
user whose own department a batch belongs to (or COA/ADMIN for all); actually
resolving a review item or registering a mapping is COA/ADMIN only — those
are corrective, cross-cutting actions, not something a desk role does day to
day.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import models
from audit import log
from database import get_db
from pipeline import id_mapping
from tz_utils import utc_now

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

DEPT_ROLES = ("ENG", "TD", "SNT")


@router.get("/batches")
def list_batches(limit: int = 50, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    query = db.query(models.IngestionBatch)
    if current_user.role in DEPT_ROLES:
        query = query.filter(models.IngestionBatch.department == current_user.department)
    rows = query.order_by(models.IngestionBatch.created_at.desc()).limit(limit).all()
    return [
        {
            "batch_id": b.batch_id,
            "source_system": b.source_system,
            "department": b.department,
            "filename": b.filename,
            "rows_in": b.rows_in,
            "rows_valid": b.rows_valid,
            "rows_rejected": b.rows_rejected,
            "rows_review": b.rows_review,
            "created_at": b.created_at,
            "created_by": b.created_by,
        }
        for b in rows
    ]


@router.get("/review-queue")
def list_review_queue(
    status: str = "pending",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    query = db.query(models.ReviewQueueItem)
    if status:
        query = query.filter(models.ReviewQueueItem.status == status)
    if current_user.role in DEPT_ROLES:
        # a department only sees review items from batches it submitted
        batch_ids = [
            b.batch_id for b in db.query(models.IngestionBatch.batch_id).filter_by(department=current_user.department)
        ]
        query = query.filter(models.ReviewQueueItem.batch_id.in_(batch_ids))
    rows = query.order_by(models.ReviewQueueItem.created_at.desc()).all()
    return [
        {
            "id": r.id,
            "batch_id": r.batch_id,
            "source_system": r.source_system,
            "raw_row": json.loads(r.raw_row_json or "{}"),
            "reason": r.reason,
            "status": r.status,
            "created_at": r.created_at,
            "resolved_by": r.resolved_by,
            "resolved_at": r.resolved_at,
        }
        for r in rows
    ]


@router.post("/review-queue/{item_id}/resolve")
def resolve_review_item(
    item_id: int,
    canonical_asset_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA", "ADMIN")),
):
    """Registers the mapping the item was stuck on, then marks it resolved.
    Does not retroactively re-import the row — the operator re-uploads the
    same file, and this time it resolves cleanly."""
    item = db.query(models.ReviewQueueItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="review item not found")
    if item.status != "pending":
        raise HTTPException(status_code=400, detail=f"item is already '{item.status}'")

    raw_row = json.loads(item.raw_row_json or "{}")
    raw_asset_id = raw_row.get("asset_id", "")
    if not raw_asset_id:
        raise HTTPException(status_code=400, detail="this review item has no asset_id to map")

    id_mapping.register_asset_mapping(db, item.source_system, raw_asset_id, canonical_asset_id)

    item.status = "resolved"
    item.resolved_by = current_user.user_id
    import datetime as dt

    item.resolved_at = utc_now()
    db.commit()

    log(
        db,
        "review_item_resolved",
        current_user.user_id,
        {"item_id": item_id, "source_system": item.source_system, "raw_asset_id": raw_asset_id, "canonical_asset_id": canonical_asset_id},
    )
    return {"id": item.id, "status": item.status, "mapped_to": canonical_asset_id}


@router.get("/asset-mappings")
def list_asset_mappings(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    rows = db.query(models.AssetIdMapping).all()
    return [
        {"source_system": m.source_system, "source_asset_id": m.source_asset_id, "canonical_asset_id": m.canonical_asset_id}
        for m in rows
    ]


@router.post("/asset-mappings")
def create_asset_mapping(
    source_system: str,
    source_asset_id: str,
    canonical_asset_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA", "ADMIN")),
):
    if not db.query(models.AssetCriticality).filter_by(asset_id=canonical_asset_id).first():
        raise HTTPException(status_code=400, detail=f"canonical_asset_id '{canonical_asset_id}' is not a registered asset")
    id_mapping.register_asset_mapping(db, source_system.upper(), source_asset_id, canonical_asset_id)
    log(db, "asset_mapping_registered", current_user.user_id, {"source_system": source_system, "source_asset_id": source_asset_id, "canonical_asset_id": canonical_asset_id})
    return {"source_system": source_system.upper(), "source_asset_id": source_asset_id, "canonical_asset_id": canonical_asset_id}


@router.get("/corridor-mappings")
def list_corridor_mappings(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    rows = db.query(models.CorridorIdMapping).all()
    return [
        {"source_system": m.source_system, "source_corridor_id": m.source_corridor_id, "canonical_corridor_id": m.canonical_corridor_id}
        for m in rows
    ]


@router.post("/corridor-mappings")
def create_corridor_mapping(
    source_system: str,
    source_corridor_id: str,
    canonical_corridor_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA", "ADMIN")),
):
    id_mapping.register_corridor_mapping(db, source_system.upper(), source_corridor_id, canonical_corridor_id)
    log(db, "corridor_mapping_registered", current_user.user_id, {"source_system": source_system, "source_corridor_id": source_corridor_id, "canonical_corridor_id": canonical_corridor_id})
    return {"source_system": source_system.upper(), "source_corridor_id": source_corridor_id, "canonical_corridor_id": canonical_corridor_id}
