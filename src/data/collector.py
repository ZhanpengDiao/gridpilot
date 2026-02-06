"""Collects and aggregates all data sources into a single snapshot."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from src.api.amber import AmberClient
from src.api.aemo import AEMOClient
from src.api.weather import WeatherClient
from src.core.config import Config
from src.models.types import (
    BatteryState,
    GridState,
    PriceChannel,
    PriceDescriptor,
    PriceInterval,
    SolarForecast,
    SpikeStatus,
    UsageInterval,
)

logger = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Complete system state at a point in time."""
    timestamp: datetime
    # Prices — 5-min intervals
    current_import_price: PriceInterval | None
    current_export_price: PriceInterval | None
    price_forecast: list[PriceInterval]       # future intervals only
    price_history: list[PriceInterval]         # today's actuals
    # Battery
    battery: BatteryState
    # Solar
    solar_forecast: list[SolarForecast]
    current_solar_kw: float
    # Grid
    grid_state: GridState
    # Household — derived from recent usage
    predicted_load_kw: float
    recent_usage: list[UsageInterval]
    # VPP
    vpp_event_active: bool
    # Meta
    interval_minutes: int                      # 5 for this site
    tariff_period: str                         # offPeak, shoulder, peak
    tariff_season: str                         # summer, winter, etc.
    descriptor: str                            # Amber's price descriptor


class DataCollector:
    def __init__(self, config: Config):
        self._config = config
        self._amber = AmberClient(config.amber_api_token, config.amber_site_id)
        self._weather = WeatherClient(config.latitude, config.longitude)
        self._aemo = AEMOClient(config.nem_region)
        self._usage_cache: list[UsageInterval] = []

    async def collect(self) -> Snapshot:
        """Gather all data sources into a single snapshot."""
        current_task = self._amber.get_current_prices()
        forecast_task = self._amber.get_price_forecast()
        battery_task = self._amber.get_battery_state(self._config)
        solar_task = self._weather.get_solar_forecast()
        grid_task = self._aemo.get_grid_state()

        results = await asyncio.gather(
            current_task, forecast_task, battery_task, solar_task, grid_task,
            return_exceptions=True,
        )
        current_prices, all_prices, battery, solar, grid = results

        # Handle failures
        if isinstance(current_prices, Exception):
            logger.error("Amber current prices failed: %s", current_prices)
            current_prices = []
        if isinstance(all_prices, Exception):
            logger.error("Amber forecast failed: %s", all_prices)
            all_prices = []
        if isinstance(battery, Exception):
            logger.error("Battery state failed: %s", battery)
            battery = self._default_battery()
        if isinstance(solar, Exception):
            logger.error("Solar forecast failed: %s", solar)
            solar = []
        if isinstance(grid, Exception):
            logger.error("AEMO grid failed: %s", grid)
            grid = GridState(datetime.now(), self._config.nem_region, 0, 0, 0, 0)

        # Split forecast into history + future
        now = datetime.now().astimezone()
        history = [p for p in all_prices if p.interval_type == "ActualInterval"]
        forecast = [p for p in all_prices if p.interval_type == "ForecastInterval"]

        # Current prices by channel
        import_price = next(
            (p for p in current_prices if p.channel == PriceChannel.GENERAL), None
        )
        export_price = next(
            (p for p in current_prices if p.channel == PriceChannel.FEED_IN), None
        )

        # Solar estimate from nearest forecast hour
        current_solar = solar[0].generation_kw if solar else 0

        # Load prediction from recent usage history
        predicted_load = self._predict_load_from_history(history)

        # VPP: detect from spike status on feed-in channel
        vpp_active = any(
            p.spike_status == SpikeStatus.ACTUAL and p.channel == PriceChannel.FEED_IN
            for p in current_prices
        )

        # Tariff info from current price
        tariff_period = "unknown"
        tariff_season = "unknown"
        descriptor = "neutral"
        if import_price:
            if import_price.tariff:
                tariff_period = import_price.tariff.period.value
                tariff_season = import_price.tariff.season.value
            descriptor = import_price.descriptor.value

        return Snapshot(
            timestamp=datetime.now(),
            current_import_price=import_price,
            current_export_price=export_price,
            price_forecast=forecast,
            price_history=history,
            battery=battery,
            solar_forecast=solar,
            current_solar_kw=current_solar,
            grid_state=grid,
            predicted_load_kw=predicted_load,
            recent_usage=[],
            vpp_event_active=vpp_active,
            interval_minutes=5,
            tariff_period=tariff_period,
            tariff_season=tariff_season,
            descriptor=descriptor,
        )

    def _predict_load_from_history(self, history: list[PriceInterval]) -> float:
        """Estimate current load from recent actual usage patterns.
        Falls back to time-based heuristic if no history.
        """
        # Use recent general channel actuals to estimate consumption trend
        recent_general = [
            p for p in history[-12:]  # last hour of 5-min intervals
            if p.channel == PriceChannel.GENERAL
        ]
        if len(recent_general) >= 3:
            # Amber perKwh * usage would give cost, but we don't have kWh in price data.
            # Fall back to time-based for now — usage endpoint needed for real load.
            pass

        hour = datetime.now().hour
        weekday = datetime.now().weekday() < 5
        if 6 <= hour < 9:
            return 2.5 if weekday else 1.5   # morning routine
        elif 9 <= hour < 16:
            return 0.8 if weekday else 1.5   # daytime
        elif 16 <= hour < 21:
            return 3.5                        # evening peak
        elif 21 <= hour < 24:
            return 1.5                        # wind down
        else:
            return 0.5                        # overnight

    def _default_battery(self) -> BatteryState:
        c = self._config
        return BatteryState(
            soc_pct=50, soc_kwh=c.battery_capacity_kwh * 0.5,
            capacity_kwh=c.battery_capacity_kwh,
            max_charge_kw=c.battery_max_charge_kw,
            max_discharge_kw=c.battery_max_discharge_kw,
            round_trip_efficiency=c.battery_round_trip_efficiency,
            cycle_cost_cents=c.battery_cycle_cost_cents,
            min_soc_pct=c.battery_min_soc_pct,
        )

    async def close(self):
        await self._amber.close()
        await self._weather.close()
        await self._aemo.close()
