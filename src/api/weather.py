"""Open-Meteo weather API for solar generation forecasting."""
import logging
from datetime import datetime

import httpx

from src.models.types import SolarForecast

logger = logging.getLogger(__name__)


class WeatherClient:
    """Free weather API — no key required."""

    BASE_URL = "https://api.open-meteo.com/v1"

    def __init__(self, latitude: float, longitude: float):
        self._lat = latitude
        self._lon = longitude
        self._http = httpx.AsyncClient(timeout=15)

    async def get_solar_forecast(self, hours: int = 48) -> list[SolarForecast]:
        """Get hourly solar irradiance and weather forecast."""
        resp = await self._http.get(
            f"{self.BASE_URL}/forecast",
            params={
                "latitude": self._lat,
                "longitude": self._lon,
                "hourly": "direct_radiation,cloud_cover,temperature_2m",
                "forecast_hours": hours,
                "timezone": "auto",
            },
        )
        resp.raise_for_status()
        data = resp.json()["hourly"]

        forecasts = []
        for i, time_str in enumerate(data["time"]):
            irradiance = data["direct_radiation"][i] or 0
            # Rough conversion: irradiance (W/m²) → kW for typical residential system
            # Assumes ~6.6kW system, ~15% panel efficiency, ~20m² effective area
            estimated_kw = irradiance * 20 / 1000 * 0.15
            forecasts.append(SolarForecast(
                timestamp=datetime.fromisoformat(time_str),
                generation_kw=round(estimated_kw, 2),
                cloud_cover_pct=data["cloud_cover"][i] or 0,
                temperature_c=data["temperature_2m"][i] or 25,
            ))
        return forecasts

    async def close(self):
        await self._http.aclose()
