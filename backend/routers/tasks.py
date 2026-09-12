"""Task submission, import, listing, and flagging endpoints.

RBAC (enforced server-side via auth.require_role / auth.get_current_user —
never by trusting client-supplied headers or query params):
  POST /api/tasks             ENG/TD/SNT only; payload.department MUST match
                               the caller's own department (a TD user cannot
                               submit as ENG).
  GET  /api/tasks              any authenticated user, but ROW-LEVEL scoped:
                               ENG/TD/SNT callers only ever get their own
                               department's rows, even if they pass
                               ?department=<other> — that query param is
                               simply ignored for them, not merely defaulted.
                               COA/ADMIN see everything and may filter.
  POST /api/tasks/import        same rule as POST /api/tasks, extended to
                                 bulk import: the department query param must
                                 match the caller's own department.
  POST .../flag-safety,
  .../flag-interlocking,
  .../mutual-exclusion            the caller must own the task's department
                                   (ENG/TD/SNT) or be COA/ADMIN (elevated
                                   override, e.g. Control Office escalating a
                                   safety concern raised outside the normal
                                   desk flow).
"""
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

import auth
import models
import priority_engine
import schemas
from audit import log
from database import get_db
from importers import csv_importer, templates

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

DEPT_ROLES = ("ENG", "TD", "SNT")
ELEVATED_ROLES = ("COA", "ADMIN")


def _check_task_ownership(user: models.User, task_department: str):
    """Used for actions on an EXISTING task (flagging, mutual exclusion):
    a department user may only act on their own department's tasks; COA/ADMIN
    may act on any (elevated override)."""
    if user.role in DEPT_ROLES and user.department != task_department:
        raise HTTPException(status_code=403, detail=f"role {user.role} may not act on department {task_department}'s data")


def _new_task_id(department: str) -> str:
    return f"task-{department}-{uuid.uuid4().hex[:8]}"


@router.post("", response_model=schemas.TaskOut)
def create_task(
    payload: schemas.TaskCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role(*DEPT_ROLES)),
):
    department = payload.department.upper()
    if department != current_user.department:
        raise HTTPException(
            status_code=403,
            detail=f"you are {current_user.department}; you may not submit a task for department '{department}'",
        )

    task = models.MaintenanceTask(
        task_id=_new_task_id(department),
        department=department,
        asset_id=payload.asset_id,
        defect_type=payload.defect_type,
        severity=payload.severity,
        overdue_days=payload.overdue_days,
        required_duration_hours=payload.required_duration_hours,
        corridor_id=payload.corridor_id,
        safety_critical=payload.safety_critical,
        interlocking_critical=payload.interlocking_critical,
        mutually_exclusive_with=",".join(payload.mutually_exclusive_with or []),
        status="submitted",
        source=payload.source or "manual",
        source_ref=payload.source_ref or "",
        created_by=current_user.user_id,
    )
    priority_engine.rescore_task(db, task)

    db.add(task)
    db.commit()
    db.refresh(task)

    log(db, "task_submitted", current_user.user_id, {"task_id": task.task_id, "department": task.department, "score": task.priority_score})
    return task


@router.get("", response_model=list[schemas.TaskOut])
def list_tasks(
    department: str = None,
    status: str = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    query = db.query(models.MaintenanceTask)

    if current_user.role in DEPT_ROLES:
        # Row-level scoping: a client-supplied ?department= is never trusted
        # for a department role, even if it names their own department — the
        # filter is always derived from the authenticated identity.
        query = query.filter(models.MaintenanceTask.department == current_user.department)
    elif department:
        query = query.filter(models.MaintenanceTask.department == department.upper())

    if status:
        query = query.filter(models.MaintenanceTask.status == status)

    return query.order_by(models.MaintenanceTask.priority_score.desc()).all()


@router.get("/template")
def download_template(department: str = "ENG"):
    # Deliberately public (no auth dependency): this is a static column
    # template with a sample row, not real operational data, and it's
    # fetched via a plain <a href> download link in the UI, which cannot
    # attach an Authorization header. Nothing sensitive is served here.
    csv_text = templates.template_csv(department)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={department.upper()}_task_import_template.csv"},
    )


@router.post("/import")
async def import_tasks(
    department: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role(*DEPT_ROLES)),
):
    department = department.upper()
    if department != current_user.department:
        raise HTTPException(
            status_code=403,
            detail=f"you are {current_user.department}; you may not import tasks for department '{department}'",
        )
    content = await file.read()
    source_system = csv_importer.DEPARTMENT_SOURCE_SYSTEM.get(department, "CSV")

    try:
        result = csv_importer.parse_upload(file.filename, content, department, db, source_system=source_system)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not parse file: {e}")

    valid_rows = result["valid_tasks"]
    row_errors = result["row_errors"]
    review_rows = result["review_rows"]

    created = []
    for row in valid_rows:
        task = models.MaintenanceTask(
            task_id=_new_task_id(row["department"]),
            source="csv",
            source_ref=file.filename,
            created_by=current_user.user_id,
            status="submitted",
            **row,
        )
        priority_engine.rescore_task(db, task)
        db.add(task)
        created.append(task.task_id)

    batch_id = f"batch-{uuid.uuid4().hex[:10]}"
    db.add(
        models.IngestionBatch(
            batch_id=batch_id,
            source_system=source_system,
            department=department,
            filename=file.filename,
            rows_in=result["rows_in"],
            rows_valid=len(created),
            rows_rejected=len(row_errors),
            rows_review=len(review_rows),
            created_by=current_user.user_id,
        )
    )
    for rr in review_rows:
        db.add(
            models.ReviewQueueItem(
                batch_id=batch_id,
                source_system=source_system,
                raw_row_json=__import__("json").dumps(rr["raw_row"]),
                reason=rr["reason"],
            )
        )

    db.commit()
    log(
        db,
        "task_csv_import",
        current_user.user_id,
        {
            "batch_id": batch_id,
            "filename": file.filename,
            "department": department,
            "rows_in": result["rows_in"],
            "duplicates_dropped": result["duplicates_dropped"],
            "created": len(created),
            "rejected": len(row_errors),
            "needs_review": len(review_rows),
        },
    )

    return {
        "batch_id": batch_id,
        "filename": file.filename,
        "department": department,
        "source_system": source_system,
        "rows_in": result["rows_in"],
        "duplicates_dropped": result["duplicates_dropped"],
        "imported_count": len(created),
        "imported_task_ids": created,
        "rejected_count": len(row_errors),
        "row_errors": row_errors,
        "review_count": len(review_rows),
        "review_rows": [{"row": r["row"], "reason": r["reason"]} for r in review_rows],
    }


@router.post("/{task_id}/flag-safety", response_model=schemas.TaskOut)
def flag_safety(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    task = db.query(models.MaintenanceTask).filter_by(task_id=task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    _check_task_ownership(current_user, task.department)

    task.safety_critical = True
    priority_engine.rescore_task(db, task)
    db.commit()
    db.refresh(task)
    log(db, "task_flag_safety", current_user.user_id, {"task_id": task_id})
    return task


@router.post("/{task_id}/flag-interlocking", response_model=schemas.TaskOut)
def flag_interlocking(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    task = db.query(models.MaintenanceTask).filter_by(task_id=task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.department != "SNT":
        raise HTTPException(status_code=400, detail="interlocking-critical flag only applies to S&T tasks")
    _check_task_ownership(current_user, task.department)

    task.interlocking_critical = True
    priority_engine.rescore_task(db, task)
    db.commit()
    db.refresh(task)
    log(db, "task_flag_interlocking", current_user.user_id, {"task_id": task_id})
    return task


@router.post("/{task_id}/mutual-exclusion", response_model=schemas.TaskOut)
def set_mutual_exclusion(
    task_id: str,
    payload: schemas.MutualExclusionRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    task = db.query(models.MaintenanceTask).filter_by(task_id=task_id).first()
    other = db.query(models.MaintenanceTask).filter_by(task_id=payload.other_task_id).first()
    if not task or not other:
        raise HTTPException(status_code=404, detail="task not found")
    _check_task_ownership(current_user, task.department)

    def add_link(a, b_id):
        existing = set(x.strip() for x in (a.mutually_exclusive_with or "").split(",") if x.strip())
        existing.add(b_id)
        a.mutually_exclusive_with = ",".join(sorted(existing))

    add_link(task, other.task_id)
    add_link(other, task.task_id)
    db.commit()
    db.refresh(task)
    log(db, "task_mutual_exclusion_set", current_user.user_id, {"task_id": task_id, "other_task_id": payload.other_task_id})
    return task
