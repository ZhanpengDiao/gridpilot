"""Collects and aggregates all data sources into a single snapshot."""
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
    HouseholdLoad,
    PriceChannel,
    PriceInterval,
    SolarForecast,
)

logger = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Complete system state at a point in time."""
    timestamp: datetime
    # Prices
    current_import_price: PriceInterval | None
    current_export_price: PriceInterval | None
    price_forecast: list[PriceInterval]
    # Battery
    battery: BatteryState
    # Solar
    solar_forecast: list[SolarForecast]
    current_solar_kw: float
    # Grid
    grid_state: GridState
    # Household
    predicted_load_kw: float
    # VPP
    vpp_event_active: bool


class DataCollector:
    def __init__(self, config: Config):
        self._config = config
        self._amber = AmberClient(config.amber_api_token, config.amber_site_id)
        self._weather = WeatherClient(config.latitude, config.longitude)
        self._aemo = AEMOClient(config.nem_region)

    async def collect(self) -> Snapshot:
        """Gather all data sources into a single snapshot."""
        import asyncio

        prices_task = self._amber.get_current_prices()
        forecast_task = self._amber.get_price_forecast()
        battery_task = self._amber.get_battery_state(self._config)
        solar_task = self._weather.get_solar_forecast()
        grid_task = self._aemo.get_grid_state()

        prices, forecast, battery, solar, grid = await asyncio.gather(
            prices_task, forecast_task, battery_task, solar_task, grid_task,
            return_exceptions=True,
        )

        # Handle failures gracefully
        if isinstance(prices, Exception):
            logger.error("Amber prices failed: %s", prices)
            prices = []
        if isinstance(forecast, Exception):
            logger.error("Amber forecast failed: %s", forecast)
            forecast = []
        if isinstance(battery, Exception):
            logger.error("Battery state failed: %s", battery)
            battery = self._default_battery()
        if isinstance(solar, Exception):
            logger.error("Solar forecast failed: %s", solar)
            solar = []
        if isinstance(grid, Exception):
            logger.error("AEMO grid failed: %s", grid)
            grid = GridState(datetime.now(), self._config.nem_region, 0, 0, 0, 0)

        import_price = next(
            (p for p in prices if p.channel == PriceChannel.GENERAL), None
        )
        export_price = next(
            (p for p in prices if p.channel == PriceChannel.FEED_IN), None
        )

        # Current solar from nearest forecast hour
        current_solar = solar[0].generation_kw if solar else 0

        # VPP detection: Amber signals via spike or special channel
        vpp_active = any(
            p.spike_status.value == "actual" and p.channel == PriceChannel.FEED_IN
            for p in prices
        )

        return Snapshot(
            timestamp=datetime.now(),
            current_import_price=import_price,
            current_export_price=export_price,
            price_forecast=forecast,
            battery=battery,
            solar_forecast=solar,
            current_solar_kw=current_solar,
            grid_state=grid,
            predicted_load_kw=self._predict_load(),
            vpp_event_active=vpp_active,
        )

    def _predict_load(self) -> float:
        """Simple time-based load prediction. TODO: learn from historical usage."""
        hour = datetime.now().hour
        if 6 <= hour < 9:
            return 2.0    # morning routine
        elif 9 <= hour < 16:
            return 0.8    # daytime (at work)
        elif 16 <= hour < 21:
            return 3.5    # evening peak
        elif 21 <= hour < 24:
            return 1.5    # wind down
        else:
            return 0.5    # overnight

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
