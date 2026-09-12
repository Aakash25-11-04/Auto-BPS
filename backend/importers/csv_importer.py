"""CSV/Excel bulk import — the ingestion + validation stages of the Layer 2
pipeline (see backend/pipeline/ for cleaning, normalization, and ID mapping,
which this module calls into before persisting anything).

Never trusts the file blindly: every row is validated independently and bad
rows are reported back with the exact reason, while good rows in the same
file still get imported. An unresolvable asset_id is no longer a hard
rejection — it goes to the review queue instead (see pipeline/id_mapping.py).
"""
import datetime as dt
import io

import pandas as pd
from sqlalchemy.orm import Session

import models
from pipeline import cleaning, id_mapping, normalization

VALID_DEPARTMENTS = {"ENG", "TD", "SNT"}
TRUE_STRINGS = {"true", "1", "yes", "y"}
FALSE_STRINGS = {"false", "0", "no", "n", ""}

DEPARTMENT_SOURCE_SYSTEM = {"ENG": "TMS", "TD": "TDMS", "SNT": "SMMS"}


def _to_bool(value, field, errors):
    s = str(value).strip().lower() if value is not None and str(value).strip().lower() != "nan" else ""
    if s in TRUE_STRINGS:
        return True
    if s in FALSE_STRINGS:
        return False
    errors.append(f"'{field}' value '{value}' is not a recognizable boolean (use true/false)")
    return False


def _clean(value):
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() == "nan" else s


def parse_rows(df: pd.DataFrame, department: str, db: Session, source_system: str = None):
    """Returns (valid_tasks, row_errors, review_rows).
    row_errors: hard validation failures (bad severity, malformed date, ...).
    review_rows: otherwise-valid rows whose asset_id could not be resolved
        to a canonical asset — held for human review, never silently
        dropped and never silently guessed at (see pipeline/id_mapping.py).
    """
    source_system = source_system or DEPARTMENT_SOURCE_SYSTEM.get(department, "CSV")
    valid_tasks = []
    row_errors = []
    review_rows = []

    for idx, raw in df.iterrows():
        row_num = idx + 2  # +1 for 0-index, +1 for header line
        errors = []
        row = {k: _clean(v) for k, v in raw.to_dict().items()}
        row = normalization.normalize_row(row)

        asset_id_raw = row.get("asset_id", "")
        defect_type = row.get("defect_type", "")
        corridor_id_raw = row.get("corridor_id", "")

        canonical_asset_id = None
        if not asset_id_raw:
            errors.append("asset_id is required")
        else:
            canonical_asset_id = id_mapping.resolve_asset_id(db, source_system, asset_id_raw)
            if canonical_asset_id is None:
                review_rows.append(
                    {
                        "row": row_num,
                        "reason": f"asset_id '{asset_id_raw}' has no known mapping from {source_system} to a "
                        "canonical asset — register a mapping under Admin, or register the asset directly, "
                        "then re-import",
                        "raw_row": row,
                    }
                )
                continue

        if not defect_type:
            errors.append("defect_type is required")
        if not corridor_id_raw:
            errors.append("corridor_id is required")

        severity_raw = row.get("severity", "")
        severity = None
        try:
            severity = int(float(severity_raw))
            if severity < 1 or severity > 5:
                errors.append(f"severity must be 1-5, got '{severity_raw}'")
                severity = None
        except (ValueError, TypeError):
            errors.append(f"severity '{severity_raw}' is not a valid integer 1-5")

        duration_raw = row.get("required_duration_hours", "")
        required_duration_hours = None
        try:
            required_duration_hours = float(duration_raw)
            if required_duration_hours <= 0:
                errors.append(f"required_duration_hours must be > 0, got '{duration_raw}'")
                required_duration_hours = None
        except (ValueError, TypeError):
            errors.append(f"required_duration_hours '{duration_raw}' is not a valid number")

        reported_date_raw = row.get("reported_date", "")
        overdue_days_raw = row.get("overdue_days", "")
        overdue_days = None
        if reported_date_raw:
            try:
                reported_date = dt.datetime.strptime(reported_date_raw, "%Y-%m-%d").date()
                today = dt.date.today()
                if reported_date > today:
                    errors.append(f"reported_date '{reported_date_raw}' is in the future")
                else:
                    overdue_days = (today - reported_date).days
            except ValueError:
                errors.append(f"reported_date '{reported_date_raw}' is malformed, expected YYYY-MM-DD")
        else:
            try:
                overdue_days = int(float(overdue_days_raw)) if overdue_days_raw else 0
                if overdue_days < 0:
                    errors.append(f"overdue_days must be >= 0, got '{overdue_days_raw}'")
                    overdue_days = None
            except (ValueError, TypeError):
                errors.append(f"overdue_days '{overdue_days_raw}' is not a valid integer")

        safety_critical = _to_bool(row.get("safety_critical", ""), "safety_critical", errors)
        interlocking_critical = _to_bool(row.get("interlocking_critical", ""), "interlocking_critical", errors)

        if errors:
            row_errors.append({"row": row_num, "errors": errors})
            continue

        corridor_id = id_mapping.resolve_corridor_id(db, source_system, corridor_id_raw, strict=False)

        valid_tasks.append(
            {
                "department": department,
                "asset_id": canonical_asset_id,
                "defect_type": defect_type,
                "severity": severity,
                "overdue_days": overdue_days,
                "required_duration_hours": required_duration_hours,
                "corridor_id": corridor_id,
                "safety_critical": safety_critical,
                "interlocking_critical": interlocking_critical,
                "mutually_exclusive_with": row.get("mutually_exclusive_with", ""),
            }
        )

    return valid_tasks, row_errors, review_rows


def parse_upload(filename: str, content: bytes, department: str, db: Session, source_system: str = None):
    if department.upper() not in VALID_DEPARTMENTS:
        raise ValueError(f"unknown department '{department}', expected one of {sorted(VALID_DEPARTMENTS)}")

    if filename.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(content), dtype=str)
    else:
        df = pd.read_csv(io.BytesIO(content), dtype=str, comment="#", keep_default_na=True)

    df.columns = [c.strip() for c in df.columns]
    rows_in = len(df)
    df, duplicates_dropped = cleaning.deduplicate(df)

    valid_tasks, row_errors, review_rows = parse_rows(df, department.upper(), db, source_system)
    return {
        "rows_in": rows_in,
        "duplicates_dropped": duplicates_dropped,
        "valid_tasks": valid_tasks,
        "row_errors": row_errors,
        "review_rows": review_rows,
    }
