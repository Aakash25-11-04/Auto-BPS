"""Weather adapter (Fix 4) — Open-Meteo, hourly, real, and never fabricated.

Open-Meteo (https://open-meteo.com) is free for non-commercial use, needs
NO API key and no signup, covers all of India, and serves hourly forecasts
up to 16 days. Verified live on this build with the exact call below.

HOURLY, not daily, is the whole point: a maintenance window is a few hours,
and "thunderstorm somewhere today" is a different statement from
"thunderstorm 04:00-05:00 IST". Measured on this build for Delhi
(28.6139, 77.209): WMO code 95 (thunderstorm) at exactly 04:00 and 05:00
IST with the rest of the day clear — a daily roll-up would have excluded a
whole day of otherwise usable windows.

THERE IS NO SYNTHETIC FALLBACK. A previous revision of this file shipped a
SyntheticWeatherAdapter that generated plausible-looking values whenever
the real provider was unreachable; that has been deleted outright. If
Open-Meteo cannot be reached, this raises, the caller reports weather as
UNAVAILABLE, and the UI says so — fabricating weather for a system that
decides whether it is safe to send a crew up an OHE mast is not an
acceptable failure mode, however clearly it is labelled.
"""
import datetime as dt
from typing import List

import requests

from adapters.base import WeatherSourceAdapter
from tz_utils import IST, UTC

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes (https://open-meteo.com/en/docs):
# 95 thunderstorm, 96/99 thunderstorm with hail — the provider's signal for
# lightning risk. 45/48 are fog / depositing rime fog.
THUNDERSTORM_CODES = {95, 96, 99}
FOG_CODES = {45, 48}
FOG_VISIBILITY_KM_THRESHOLD = 2.0

HOURLY_FIELDS = [
    "temperature_2m",
    "precipitation_probability",
    "rain",
    "wind_speed_10m",
    "visibility",
    "weather_code",
]


class WeatherProviderUnavailable(RuntimeError):
    """Raised when real weather cannot be obtained. Callers must surface
    this as 'unavailable' — never substitute invented values."""


class OpenMeteoAdapter(WeatherSourceAdapter):
    provider_name = "open_meteo"

    def fetch_forecast(self, lat: float, lon: float, days: int = 7) -> List[dict]:
        """Returns one dict per FORECAST HOUR. Times come back from the
        provider already in IST (timezone=Asia/Kolkata) and are converted
        once, here, to this codebase's naive-but-UTC storage convention —
        so the IST timetable data and the IST weather data are never
        double-converted against each other."""
        days = max(1, min(days, 16))  # Open-Meteo's own forecast_days ceiling
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(HOURLY_FIELDS),
            "forecast_days": days,
            "timezone": "Asia/Kolkata",
        }
        try:
            resp = requests.get(OPEN_METEO_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise WeatherProviderUnavailable(f"Open-Meteo request failed ({OPEN_METEO_URL}): {e}") from e

        hourly = data.get("hourly")
        if not hourly or "time" not in hourly:
            raise WeatherProviderUnavailable(f"unexpected Open-Meteo response shape: {sorted(data.keys())}")

        def col(name):
            return hourly.get(name) or [None] * len(hourly["time"])

        temps, precip, rain = col("temperature_2m"), col("precipitation_probability"), col("rain")
        wind, vis, codes = col("wind_speed_10m"), col("visibility"), col("weather_code")

        rows = []
        for i, stamp in enumerate(hourly["time"]):
            # Provider returns local (IST) wall-clock timestamps because we
            # asked for timezone=Asia/Kolkata; attach IST explicitly and
            # convert to the naive-but-UTC instant used for storage.
            valid_ist = dt.datetime.fromisoformat(stamp).replace(tzinfo=IST)
            valid_utc = valid_ist.astimezone(UTC).replace(tzinfo=None)
            code = codes[i]
            visibility_km = (vis[i] / 1000.0) if vis[i] is not None else None
            rows.append(
                {
                    "valid_time": valid_utc,
                    "temperature_c": temps[i],
                    "precipitation_probability_pct": precip[i],
                    "rain_mm": rain[i],
                    "wind_speed_kmh": wind[i],
                    "visibility_km": round(visibility_km, 2) if visibility_km is not None else None,
                    "weather_code": code,
                    "lightning_risk": code in THUNDERSTORM_CODES if code is not None else False,
                    "fog_risk": bool(
                        (code in FOG_CODES if code is not None else False)
                        or (visibility_km is not None and visibility_km < FOG_VISIBILITY_KM_THRESHOLD)
                    ),
                    "source": self.provider_name,
                    "latitude": data.get("latitude", lat),
                    "longitude": data.get("longitude", lon),
                }
            )
        return rows

    def health_check(self) -> dict:
        try:
            resp = requests.get(
                OPEN_METEO_URL,
                params={"latitude": 28.6139, "longitude": 77.2090, "hourly": "temperature_2m", "forecast_days": 1,
                        "timezone": "Asia/Kolkata"},
                timeout=8,
            )
            resp.raise_for_status()
            return {"provider": self.provider_name, "connected": True, "reason": "reachable", "url": OPEN_METEO_URL}
        except Exception as e:
            return {"provider": self.provider_name, "connected": False, "reason": str(e), "url": OPEN_METEO_URL}


# Single real provider. Deliberately no synthetic entry — see module
# docstring: unreachable weather is reported as unavailable, never invented.
WEATHER_ADAPTERS = {"open_meteo": OpenMeteoAdapter()}
