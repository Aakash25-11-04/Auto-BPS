"""Downloadable CSV import templates, one per department."""

COLUMNS = [
    "asset_id",
    "defect_type",
    "severity",
    "reported_date",
    "overdue_days",
    "required_duration_hours",
    "corridor_id",
    "safety_critical",
    "interlocking_critical",
    "mutually_exclusive_with",
]

SAMPLE_ROWS = {
    "ENG": ["ENG-TRK-1042", "rail_fracture", "5", "2026-08-01", "", "6", "GZB-SBB", "true", "false", ""],
    "TD": ["TD-OHE-0087", "ohe_insulator_fault", "3", "", "10", "4", "GZB-SBB", "false", "false", ""],
    "SNT": ["SNT-SIG-0231", "signal_relay_fault", "4", "2026-08-20", "", "3", "GZB-SBB", "false", "true", ""],
}


def template_csv(department: str) -> str:
    dept = department.upper() if department else "ENG"
    header = ",".join(COLUMNS)
    sample = SAMPLE_ROWS.get(dept, SAMPLE_ROWS["ENG"])
    row = ",".join(sample)
    note = (
        "# reported_date is optional (YYYY-MM-DD): if given, overdue_days is computed from it "
        "and the overdue_days column is ignored. severity is 1-5. duration is in hours."
    )
    return f"{note}\n{header}\n{row}\n"
