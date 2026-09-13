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


class WeatherSourceAdapter(ABC):
    """Base class for a live adapter to a weather forecast provider — the
    7th ingested data source, following the exact same pluggable-adapter
    pattern as SourceSystemAdapter above so swapping providers later (e.g.
    IMD's own API once credentials are available) is a config change, not a
    rewrite of weather_service.py or anything downstream of it."""

    provider_name: str = "unspecified"

    @abstractmethod
    def fetch_forecast(self, lat: float, lon: float, days: int = 7) -> List[dict]:
        """Return a list of per-day forecast dicts in WeatherForecastEntry
        field shape (forecast_date, precipitation_probability_pct,
        wind_speed_kmh, visibility_km, lightning_risk, temperature_max_c,
        temperature_min_c) for the given coordinates. Must set source='real'
        and provider=self.provider_name. Raises on failure rather than
        returning a partial/fabricated result — the caller decides whether
        to fall back to the synthetic adapter, and always labels which
        happened."""
        raise NotImplementedError

    def health_check(self) -> dict:
        return {"provider": self.provider_name, "connected": False, "reason": "stub adapter, no live endpoint configured"}
