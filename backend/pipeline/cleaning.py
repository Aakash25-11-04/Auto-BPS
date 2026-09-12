"""Cleaning stage: deduplicate rows within one ingestion batch and document
how missing fields are handled (the actual missing-field defaults live in
csv_importer.parse_rows next to the fields they apply to; this module only
documents the rule and does the dedup pass, which is the one cleaning
operation that has to happen before validation, not next to it).

Documented missing-field handling rules (see csv_importer.py for the code):
  - overdue_days: defaults to 0 if neither overdue_days nor reported_date is given.
  - safety_critical / interlocking_critical: default to false if blank.
  - mutually_exclusive_with: defaults to "" (no linked tasks).
  - severity, required_duration_hours, asset_id, defect_type, corridor_id:
    have NO default — a missing value is a hard validation failure, because
    guessing at any of these could silently misprioritize or mis-schedule
    real maintenance work.
"""
import pandas as pd


def deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drops exact-duplicate rows (every column identical) within the same
    file — the same defect submitted twice in one spreadsheet, not a
    cross-batch duplicate (which is out of scope: two independently
    submitted batches reporting the same real defect are a business
    decision, not a data-cleaning one). Returns (deduped_df, rows_dropped)."""
    before = len(df)
    deduped = df.drop_duplicates(keep="first").reset_index(drop=True)
    return deduped, before - len(deduped)
