"""Amber Electric API client — matched to actual API responses."""
import logging
from datetime import datetime

import httpx

from src.models.types import (
    BatteryState,
    PriceChannel,
    PriceDescriptor,
    PriceInterval,
    SiteInfo,
    SpikeStatus,
    TariffInfo,
    TariffPeriod,
    TariffSeason,
    UsageInterval,
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

    # ── Site ──────────────────────────────────────────────

    async def get_sites(self) -> list[SiteInfo]:
        resp = await self._http.get("/sites")
        resp.raise_for_status()
        return [self._parse_site(s) for s in resp.json()]

    async def get_site(self) -> SiteInfo:
        sites = await self.get_sites()
        return next(s for s in sites if s.site_id == self._site_id)

    # ── Prices ────────────────────────────────────────────

    async def get_current_prices(self) -> list[PriceInterval]:
        """Current 5-min interval prices for all channels."""
        resp = await self._http.get(f"/sites/{self._site_id}/prices/current")
        resp.raise_for_status()
        return [self._parse_price(p) for p in resp.json()]

    async def get_price_forecast(self, next_hours: int = 48) -> list[PriceInterval]:
        """Historical + forecast prices. Returns ActualInterval, CurrentInterval, ForecastInterval."""
        # Amber returns ~376 intervals (5-min) for next=48
        resp = await self._http.get(
            f"/sites/{self._site_id}/prices",
            params={"next": next_hours},
        )
        resp.raise_for_status()
        return [self._parse_price(p) for p in resp.json()]

    # ── Usage ─────────────────────────────────────────────

    async def get_usage(self, start_date: str, end_date: str) -> list[UsageInterval]:
        """Historical usage. Dates as YYYY-MM-DD. Returns 5-min intervals.
        Note: resolution param not supported — always returns site's native interval (5min).
        """
        resp = await self._http.get(
            f"/sites/{self._site_id}/usage",
            params={"startDate": start_date, "endDate": end_date},
        )
        resp.raise_for_status()
        return [self._parse_usage(u) for u in resp.json()]

    # ── Derived state ─────────────────────────────────────

    async def get_battery_state(self, config) -> BatteryState:
        """Build battery state from config (no battery channel available from Amber)."""
        # Your site only has general + feedIn channels, no battery channel.
        # SOC must come from inverter API directly. For now, use config defaults.
        return BatteryState(
            soc_pct=50,  # TODO: get from inverter API
            soc_kwh=config.battery_capacity_kwh * 0.5,
            capacity_kwh=config.battery_capacity_kwh,
            max_charge_kw=config.battery_max_charge_kw,
            max_discharge_kw=config.battery_max_discharge_kw,
            round_trip_efficiency=config.battery_round_trip_efficiency,
            cycle_cost_cents=config.battery_cycle_cost_cents,
            min_soc_pct=config.battery_min_soc_pct,
        )

    async def get_daily_cost(self, date: str) -> dict:
        """Calculate daily cost/revenue breakdown from usage data."""
        usage = await self.get_usage(date, date)
        general = [u for u in usage if u.channel == PriceChannel.GENERAL]
        feed_in = [u for u in usage if u.channel == PriceChannel.FEED_IN]
        return {
            "date": date,
            "import_kwh": sum(u.kwh for u in general),
            "import_cost_cents": sum(u.cost_cents for u in general),
            "export_kwh": abs(sum(u.kwh for u in feed_in)),
            "export_revenue_cents": abs(sum(u.cost_cents for u in feed_in)),
            "net_cost_cents": sum(u.cost_cents for u in usage),
            "intervals": len(general),
        }

    # ── Parsers ───────────────────────────────────────────

    def _parse_site(self, data: dict) -> SiteInfo:
        return SiteInfo(
            site_id=data["id"],
            nmi=data["nmi"],
            network=data["network"],
            status=data["status"],
            active_from=data["activeFrom"],
            interval_minutes=data.get("intervalLength", 5),
            channels=data.get("channels", []),
        )

    def _parse_price(self, data: dict) -> PriceInterval:
        return PriceInterval(
            timestamp=datetime.fromisoformat(data["startTime"]),
            end_time=datetime.fromisoformat(data["endTime"]),
            per_kwh_cents=data["perKwh"],
            spot_per_kwh_cents=data.get("spotPerKwh", data["perKwh"]),
            channel=PriceChannel(data.get("channelType", "general")),
            spike_status=SpikeStatus(data.get("spikeStatus", "none")),
            descriptor=self._parse_descriptor(data.get("descriptor", "neutral")),
            renewables_pct=data.get("renewables", 0),
            tariff=self._parse_tariff(data.get("tariffInformation")),
            duration_minutes=data.get("duration", 5),
            interval_type=data.get("type", "CurrentInterval"),
            is_estimate=data.get("estimate", False),
        )

    def _parse_usage(self, data: dict) -> UsageInterval:
        return UsageInterval(
            timestamp=datetime.fromisoformat(data["startTime"]),
            end_time=datetime.fromisoformat(data["endTime"]),
            channel=PriceChannel(data.get("channelType", "general")),
            channel_id=data.get("channelIdentifier", ""),
            kwh=data.get("kwh", 0),
            cost_cents=data.get("cost", 0),
            per_kwh_cents=data.get("perKwh", 0),
            spot_per_kwh_cents=data.get("spotPerKwh", 0),
            spike_status=SpikeStatus(data.get("spikeStatus", "none")),
            descriptor=self._parse_descriptor(data.get("descriptor", "neutral")),
            renewables_pct=data.get("renewables", 0),
            tariff=self._parse_tariff(data.get("tariffInformation")),
            quality=data.get("quality", "estimated"),
        )

    def _parse_tariff(self, data: dict | None) -> TariffInfo | None:
        if not data:
            return None
        try:
            return TariffInfo(
                period=TariffPeriod(data["period"]),
                season=TariffSeason(data["season"]),
            )
        except (KeyError, ValueError):
            return None

    def _parse_descriptor(self, value: str) -> PriceDescriptor:
        try:
            return PriceDescriptor(value)
        except ValueError:
            return PriceDescriptor.NEUTRAL

    async def close(self):
        await self._http.aclose()
