"""Concrete adapter stubs for TMS (Engineering), SMMS (S&T), TDMS (Traction
Distribution) and the COA traffic planning system. Each returns an empty
result set and a clear 'not connected' health status until a live endpoint,
credentials, and field mapping are supplied for that railway's real system.
No synthetic records are ever generated here.
"""
from typing import List

from adapters.base import SourceSystemAdapter


class TMSAdapter(SourceSystemAdapter):
    """Engineering department — Track Management System."""

    system_name = "TMS"
    department = "ENG"

    def fetch_pending_tasks(self) -> List[dict]:
        return []

    def health_check(self) -> dict:
        return {"system": "TMS", "connected": False, "reason": "no TMS endpoint configured; use CSV import or manual entry"}


class SMMSAdapter(SourceSystemAdapter):
    """Signal & Telecom department — Signal Maintenance Management System."""

    system_name = "SMMS"
    department = "SNT"

    def fetch_pending_tasks(self) -> List[dict]:
        return []

    def health_check(self) -> dict:
        return {"system": "SMMS", "connected": False, "reason": "no SMMS endpoint configured; use CSV import or manual entry"}


class TDMSAdapter(SourceSystemAdapter):
    """Traction Distribution department — Traction Distribution Management System."""

    system_name = "TDMS"
    department = "TD"

    def fetch_pending_tasks(self) -> List[dict]:
        return []

    def health_check(self) -> dict:
        return {"system": "TDMS", "connected": False, "reason": "no TDMS endpoint configured; use CSV import or manual entry"}


class COAAdapter(SourceSystemAdapter):
    """Control Office — corridor availability / traffic planning system."""

    system_name = "COA"
    department = "COA"

    def fetch_pending_tasks(self) -> List[dict]:
        return []

    def health_check(self) -> dict:
        return {
            "system": "COA",
            "connected": False,
            "reason": "no COA planning API configured; corridor availability is derived from the loaded public timetable or entered manually",
        }


ADAPTERS = {
    "ENG": TMSAdapter(),
    "SNT": SMMSAdapter(),
    "TD": TDMSAdapter(),
    "COA": COAAdapter(),
}
