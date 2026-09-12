"""Pluggable adapter interface for department source systems.

Each department's maintenance defects live in a real internal system this
project has no access to (TMS/SMMS/TDMS) and the Control Office's traffic
plan lives in a COA planning system. Rather than fabricate their data, ABPS
defines the contract a live client for each system must satisfy. Dropping in
a real implementation later means writing a subclass here and registering it
— the core engine (priority scoring, optimizer, API) never changes.
"""
from abc import ABC, abstractmethod
from typing import List


class SourceSystemAdapter(ABC):
    """Base class for a live adapter to a department source system."""

    system_name: str = "unspecified"
    department: str = "unspecified"

    @abstractmethod
    def fetch_pending_tasks(self) -> List[dict]:
        """Return a list of task dicts in MaintenanceTask field shape,
        pulled from the live source system. Must set source='adapter' and
        source_ref to the originating record ID in that system."""
        raise NotImplementedError

    def health_check(self) -> dict:
        return {"system": self.system_name, "connected": False, "reason": "stub adapter, no live endpoint configured"}
