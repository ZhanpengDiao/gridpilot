"""Amber Electric API client."""
import logging
from datetime import datetime
from typing import Optional

import httpx

from src.models.types import (
    BatteryState,
    PriceChannel,
    PriceInterval,
    SpikeStatus,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.amber.com.au/v1"


class AmberClient:
    def __init__(self, api_token: str, site_id: str):
        self._site_id = site_id
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=15,
        )

    async def get_current_prices(self) -> list[PriceInterval]:
        """Get current 30-min interval prices (general + feed-in)."""
        resp = await self._http.get(f"/sites/{self._site_id}/prices/current")
        resp.raise_for_status()
        return [self._parse_price(p) for p in resp.json()]

    async def get_price_forecast(self) -> list[PriceInterval]:
        """Get forecast prices for next 12-56 hours."""
        resp = await self._http.get(
            f"/sites/{self._site_id}/prices",
            params={"resolution": 30, "next": 48},
        )
        resp.raise_for_status()
        return [self._parse_price(p) for p in resp.json()]

    async def get_usage(self, start_date: str, end_date: str) -> list[dict]:
        """Get historical usage data. Dates as YYYY-MM-DD."""
        resp = await self._http.get(
            f"/sites/{self._site_id}/usage",
            params={"startDate": start_date, "endDate": end_date, "resolution": 30},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_site_info(self) -> dict:
        """Get site configuration including battery/solar details."""
        resp = await self._http.get(f"/sites/{self._site_id}")
        resp.raise_for_status()
        return resp.json()

    async def get_battery_state(self, config) -> BatteryState:
        """Derive battery state from current usage data."""
        usage = await self.get_usage(
            datetime.now().strftime("%Y-%m-%d"),
            datetime.now().strftime("%Y-%m-%d"),
        )
        # Extract battery SOC from latest interval if available
        soc_pct = self._extract_soc(usage)
        soc_kwh = config.battery_capacity_kwh * soc_pct / 100
        return BatteryState(
            soc_pct=soc_pct,
            soc_kwh=soc_kwh,
            capacity_kwh=config.battery_capacity_kwh,
            max_charge_kw=config.battery_max_charge_kw,
            max_discharge_kw=config.battery_max_discharge_kw,
            round_trip_efficiency=config.battery_round_trip_efficiency,
            cycle_cost_cents=config.battery_cycle_cost_cents,
            min_soc_pct=config.battery_min_soc_pct,
        )

    def _extract_soc(self, usage: list[dict]) -> float:
        """Extract latest battery SOC from usage data."""
        for entry in reversed(usage):
            if entry.get("channelType") == "battery" and "soc" in entry:
                return float(entry["soc"])
        return 50.0  # fallback estimate

    def _parse_price(self, data: dict) -> PriceInterval:
        return PriceInterval(
            timestamp=datetime.fromisoformat(data["startTime"]),
            per_kwh_cents=data["perKwh"],
            spot_per_kwh_cents=data.get("spotPerKwh", data["perKwh"]),
            channel=PriceChannel(data.get("channelType", "general")),
            spike_status=SpikeStatus(data.get("spikeStatus", "none")),
            renewables_pct=data.get("renewables", 0),
            is_forecast=data.get("type") == "forecast",
        )

    async def close(self):
        await self._http.aclose()
