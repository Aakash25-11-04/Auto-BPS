"""Normalization stage: unify department codes, ID casing/whitespace, and
duration units across the three source systems before anything is matched
against a mapping table or inserted into the unified database.

Real TMS/SMMS/TDMS exports are not guaranteed to agree on any of this — one
might export "Engineering" where ABPS wants "ENG", another might report
duration in minutes instead of hours. Normalizing here, once, up front,
means every downstream stage (mapping, validation, scoring) can assume a
single canonical shape.
"""
DEPARTMENT_ALIASES = {
    "ENG": "ENG", "ENGINEERING": "ENG", "TMS": "ENG", "P.WAY": "ENG", "PWAY": "ENG",
    "TD": "TD", "TRACTION": "TD", "TRACTION DISTRIBUTION": "TD", "TDMS": "TD", "OHE": "TD",
    "SNT": "SNT", "S&T": "SNT", "SIGNAL": "SNT", "SIGNAL & TELECOM": "SNT", "SMMS": "SNT", "TELECOM": "SNT",
}


def normalize_department(raw: str) -> str:
    key = (raw or "").strip().upper()
    return DEPARTMENT_ALIASES.get(key, key)


def normalize_row(row: dict) -> dict:
    """Mutates and returns a shallow copy with normalized field values.
    Applied to every row before validation/ID-mapping."""
    out = dict(row)

    if "department" in out:
        out["department"] = normalize_department(out["department"])

    if "asset_id" in out and out["asset_id"]:
        out["asset_id"] = out["asset_id"].strip().upper()

    if "corridor_id" in out and out["corridor_id"]:
        # "ndls - gzb" / "ndls-gzb " -> "NDLS-GZB"
        out["corridor_id"] = "-".join(p.strip().upper() for p in out["corridor_id"].split("-"))

    # Unit unification: a source system may report duration in minutes via an
    # optional duration_unit column instead of ABPS's canonical hours.
    unit = (out.get("duration_unit") or "").strip().lower()
    if unit in ("minute", "minutes", "min", "mins") and out.get("required_duration_hours"):
        try:
            out["required_duration_hours"] = str(float(out["required_duration_hours"]) / 60.0)
        except ValueError:
            pass  # left as-is; validation will reject the malformed value with a clear reason

    return out
