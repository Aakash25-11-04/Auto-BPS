"""Scheduler run, plan retrieval, metrics, and human-in-the-loop approval.

RBAC: /schedule/run and /schedule/approve are COA-only (require_role("COA")
exactly — deliberately NOT COA-or-ADMIN. ADMIN owns system configuration and
users; COA owns operational scheduling decisions. Keeping these separate is
a real separation-of-duties boundary, not an oversight — an account with
"ADMIN" privileges should not automatically be able to publish a live block
plan). /schedule/plan and /schedule/comparison are viewable by any
authenticated user, but row-level scoped: a department role only ever sees
its OWN department's task-level entries/unscheduled tasks, never another
department's. Aggregate metrics (counts, hours, scores) are not
department-scoped task data and are shown to everyone.

Governance: /schedule/run always produces a new DRAFT plan version — it never
touches a previously published plan. /schedule/approve is the only endpoint
that can move a plan to 'published', and doing so supersedes whatever was
previously published for that horizon. If a run fails, nothing about the
last published plan changes; that's guaranteed simply by never writing to it.
The approving/rejecting identity is always the authenticated caller — never
a client-supplied field — so the audit trail is a real record, not decorative.
"""
import csv
import datetime as dt
import io
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

import auth
import models
import schemas
from audit import log
from baseline import run_baseline
from database import get_db
from scheduler import DEFAULT_CORRIDOR_CAPACITY, reschedule_entry, run_schedule

router = APIRouter(prefix="/api/schedule", tags=["schedule"])

DEPT_ROLES = ("ENG", "TD", "SNT")


@router.post("/run")
def run(
    horizon: str = "weekly",
    corridor_capacity: int = DEFAULT_CORRIDOR_CAPACITY,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    if horizon not in ("weekly", "monthly"):
        raise HTTPException(status_code=400, detail="horizon must be 'weekly' or 'monthly'")
    if corridor_capacity < 1:
        raise HTTPException(status_code=400, detail="corridor_capacity must be at least 1")

    try:
        result = run_schedule(db, horizon, corridor_capacity=corridor_capacity)
    except Exception as e:
        db.rollback()
        log(db, "schedule_run_failed", current_user.user_id, {"horizon": horizon, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"schedule run failed, last published plan is untouched: {e}")

    log(db, "schedule_run", current_user.user_id, {"horizon": horizon, "plan_id": result["plan_id"], "metrics": result["metrics"]})
    return result


@router.post("/entries/{entry_id}/reschedule")
def reschedule(
    entry_id: int,
    payload: schemas.RescheduleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    """FR-COA-03: manually adjust a proposed (draft-only) block allocation.
    Every constraint the optimizer itself enforces — real-train safety,
    corridor window containment, corridor capacity, mutual exclusion — is
    re-validated here; a rejected move changes nothing and returns the
    specific reason as the 400 detail, which the frontend surfaces verbatim
    and uses to snap the dragged block back to its last valid position."""
    try:
        result = reschedule_entry(db, entry_id, payload.new_start)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    log(
        db,
        "block_manually_rescheduled",
        current_user.user_id,
        {"entry_id": entry_id, "task_id": result["task_id"], "new_start": payload.new_start.isoformat()},
    )
    return result


def _scope_entries(entries: list, current_user: models.User) -> list:
    if current_user.role in DEPT_ROLES:
        return [e for e in entries if e["department"] == current_user.department]
    return entries


@router.get("/plan")
def get_plan(horizon: str = "weekly", status: str = None, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    query = db.query(models.BlockPlan).filter_by(horizon=horizon)
    if status:
        plan = query.filter_by(status=status).order_by(models.BlockPlan.version.desc()).first()
    else:
        plan = (
            query.filter(models.BlockPlan.status.in_(["published", "draft"]))
            .order_by(models.BlockPlan.version.desc())
            .first()
        )
    if not plan:
        raise HTTPException(status_code=404, detail=f"no plan exists yet for horizon '{horizon}'")

    entries = db.query(models.BlockPlanEntry).filter_by(plan_id=plan.plan_id).all()
    tasks_by_id = {t.task_id: t for t in db.query(models.MaintenanceTask).all()}

    entry_rows = []
    for e in entries:
        task = tasks_by_id.get(e.task_id)
        entry_rows.append(
            {
                "id": e.id,
                "task_id": e.task_id,
                "slot_id": e.slot_id,
                "corridor_id": e.corridor_id,
                "department": e.department,
                "assigned_window_start": e.assigned_window_start,
                "assigned_window_end": e.assigned_window_end,
                # co_scheduled_departments is deliberately kept even for a
                # row-scoped caller: it's just department CODES indicating
                # who else shares the window, not another department's
                # task-level detail (asset_id/defect_type/score), so showing
                # it doesn't violate row-level scoping and is needed for the
                # coordination UI to mean anything to a desk user.
                "co_scheduled_departments": [d for d in e.co_scheduled_departments.split(",") if d],
                "priority_score": task.priority_score if task else None,
                "priority_reason": task.priority_reason if task else None,
                "defect_type": task.defect_type if task else None,
                "asset_id": task.asset_id if task else None,
                "severity": task.severity if task else None,
                "overdue_days": task.overdue_days if task else None,
                "safety_critical": task.safety_critical if task else None,
                "interlocking_critical": task.interlocking_critical if task else None,
            }
        )
    entry_rows = _scope_entries(entry_rows, current_user)

    unscheduled_query = db.query(models.MaintenanceTask).filter(models.MaintenanceTask.status == "unscheduled")
    if current_user.role in DEPT_ROLES:
        unscheduled_query = unscheduled_query.filter(models.MaintenanceTask.department == current_user.department)
    unscheduled_rows = [
        {
            "task_id": t.task_id,
            "department": t.department,
            "corridor_id": t.corridor_id,
            "priority_score": t.priority_score,
            "reason": t.unscheduled_reason,
        }
        for t in unscheduled_query.all()
    ]

    return {
        "plan_id": plan.plan_id,
        "horizon": plan.horizon,
        "version": plan.version,
        "status": plan.status,
        "metrics": json.loads(plan.metrics_json or "{}"),
        "approved_by": plan.approved_by,
        "approved_at": plan.approved_at,
        "entries": entry_rows,
        "unscheduled": unscheduled_rows,
    }


_EXPORT_COLUMNS = [
    "plan_id", "horizon", "plan_status", "task_id", "department", "corridor_id",
    "asset_id", "defect_type", "priority_score", "assigned_window_start",
    "assigned_window_end", "co_scheduled_departments",
]


def _export_rows(db: Session, horizon: str, current_user: models.User):
    plan = (
        db.query(models.BlockPlan)
        .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
        .order_by(models.BlockPlan.version.desc())
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail=f"no plan exists yet for horizon '{horizon}'")

    entries = db.query(models.BlockPlanEntry).filter_by(plan_id=plan.plan_id).order_by(models.BlockPlanEntry.assigned_window_start).all()
    tasks_by_id = {t.task_id: t for t in db.query(models.MaintenanceTask).all()}

    if current_user.role in DEPT_ROLES:
        entries = [e for e in entries if e.department == current_user.department]

    rows = []
    for e in entries:
        task = tasks_by_id.get(e.task_id)
        rows.append(
            {
                "plan_id": plan.plan_id,
                "horizon": plan.horizon,
                "plan_status": plan.status,
                "task_id": e.task_id,
                "department": e.department,
                "corridor_id": e.corridor_id,
                "asset_id": task.asset_id if task else "",
                "defect_type": task.defect_type if task else "",
                "priority_score": f"{task.priority_score:.1f}" if task else "",
                "assigned_window_start": e.assigned_window_start.strftime("%Y-%m-%d %H:%M"),
                "assigned_window_end": e.assigned_window_end.strftime("%Y-%m-%d %H:%M"),
                "co_scheduled_departments": ",".join(d for d in e.co_scheduled_departments.split(",") if d) or "solo",
            }
        )
    return plan, rows


@router.get("/plan/export")
def export_plan(
    horizon: str = "weekly",
    format: str = "csv",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """FR-DASH-06: export the current plan (draft or published) as CSV or
    PDF — task IDs, assigned windows, departments, and co-scheduling groups.
    Row-scoped the same way GET /plan is: a department caller only ever
    exports their own department's rows."""
    if format not in ("csv", "pdf"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'pdf'")

    plan, rows = _export_rows(db, horizon, current_user)
    filename_base = f"abps_plan_{plan.plan_id}"

    if format == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        log(db, "plan_exported", current_user.user_id, {"plan_id": plan.plan_id, "format": "csv", "row_count": len(rows)})
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.csv"'},
        )

    # PDF, via reportlab — landscape letter, narrow margins, explicit column
    # widths summing to the printable width, and every cell wrapped in a
    # Paragraph so long values (plan IDs, defect types) wrap instead of
    # overflowing the page — a first pass without these left the Plan Id
    # and Co-Scheduled Departments columns clipped at the page edges.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    margin = 0.35 * inch
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(letter),
        topMargin=0.5 * inch, bottomMargin=0.5 * inch, leftMargin=margin, rightMargin=margin,
    )
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7, leading=8.5)
    header_style = ParagraphStyle("cellHeader", parent=cell_style, textColor=colors.white, fontName="Helvetica-Bold")

    col_widths_in = [1.05, 0.55, 0.55, 0.95, 0.6, 0.6, 0.8, 0.95, 0.55, 0.95, 0.95, 0.85]  # sums to ~9.65in (page usable width)
    col_widths = [w * inch for w in col_widths_in]

    header = [Paragraph(c.replace("_", " ").title(), header_style) for c in _EXPORT_COLUMNS]
    table_data = [header] + [[Paragraph(str(r[c]), cell_style) for c in _EXPORT_COLUMNS] for r in rows]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0a2a43")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f8")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements = [
        Paragraph(f"ABPS Block Plan Export &mdash; {plan.plan_id} ({plan.status})", styles["Heading2"]),
        Paragraph(f"Horizon: {plan.horizon} &middot; {len(rows)} block(s) &middot; exported by {current_user.user_id}", styles["Normal"]),
        Spacer(1, 12),
        table if rows else Paragraph("No scheduled blocks in this plan.", styles["Normal"]),
    ]
    doc.build(elements)

    log(db, "plan_exported", current_user.user_id, {"plan_id": plan.plan_id, "format": "pdf", "row_count": len(rows)})
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.pdf"'},
    )


@router.get("/metrics")
def get_metrics(horizon: str = "weekly", db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    plan = (
        db.query(models.BlockPlan)
        .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
        .order_by(models.BlockPlan.version.desc())
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail=f"no plan exists yet for horizon '{horizon}'")
    return json.loads(plan.metrics_json or "{}")


@router.get("/comparison")
def get_comparison(horizon: str = "weekly", db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """FR-COA-06: before (manual-process baseline) vs after (optimizer)
    metrics for the same horizon, computed from identical definitions
    (see metrics.py) so the comparison is genuinely apples-to-apples.
    Aggregate metrics are shown to any authenticated user; the per-entry
    baseline_entries list (used only for the frontend's dual timeline) is
    row-scoped the same way /schedule/plan is."""
    if horizon not in ("weekly", "monthly"):
        raise HTTPException(status_code=400, detail="horizon must be 'weekly' or 'monthly'")

    plan = (
        db.query(models.BlockPlan)
        .filter(models.BlockPlan.horizon == horizon, models.BlockPlan.status.in_(["published", "draft"]))
        .order_by(models.BlockPlan.version.desc())
        .first()
    )
    if not plan:
        raise HTTPException(
            status_code=404,
            detail=f"no optimized plan exists yet for horizon '{horizon}' — run the scheduler first",
        )
    optimized_metrics = json.loads(plan.metrics_json or "{}")

    baseline_result = run_baseline(db, horizon)
    baseline_metrics = baseline_result["metrics"]
    baseline_entries = _scope_entries(baseline_result["scheduled"], current_user)

    numeric_keys = [
        "tasks_total",
        "tasks_completed",
        "total_corridor_closures",
        "total_downtime_hours",
        "priority_weighted_completion",
        "block_utilization_pct",
        "coordinated_blocks",
        "avg_overdue_days_of_scheduled_tasks",
    ]
    delta = {}
    for key in numeric_keys:
        b = baseline_metrics.get(key, 0) or 0
        o = optimized_metrics.get(key, 0) or 0
        absolute = round(o - b, 2)
        percent = round((absolute / b * 100), 1) if b else (0.0 if o == 0 else 100.0)
        delta[key] = {"absolute": absolute, "percent": percent}

    return {
        "horizon": horizon,
        "baseline": baseline_metrics,
        "optimized": optimized_metrics,
        "delta": delta,
        "baseline_entries": baseline_entries,
        "optimized_plan_id": plan.plan_id,
        "optimized_plan_status": plan.status,
    }


@router.post("/approve")
def approve(
    payload: schemas.ApprovalRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("COA")),
):
    if payload.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")

    plan = db.query(models.BlockPlan).filter_by(plan_id=payload.plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan.status != "draft":
        raise HTTPException(status_code=400, detail=f"plan is '{plan.status}', only a draft plan can be approved or rejected")

    if payload.decision == "approve":
        previously_published = (
            db.query(models.BlockPlan)
            .filter_by(horizon=plan.horizon, status="published")
            .all()
        )
        for p in previously_published:
            p.status = "superseded"
        plan.status = "published"
        plan.approved_by = current_user.user_id
        plan.approved_at = dt.datetime.utcnow()
        action = "schedule_plan_approved"
    else:
        plan.status = "rejected"
        action = "schedule_plan_rejected"

    db.commit()
    log(db, action, current_user.user_id, {"plan_id": plan.plan_id, "horizon": plan.horizon, "version": plan.version})

    return {"plan_id": plan.plan_id, "status": plan.status, "horizon": plan.horizon, "version": plan.version}
