from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class BatteryAction(Enum):
    CHARGE_GRID = "charge_grid"
    CHARGE_SOLAR = "charge_solar"
    DISCHARGE_GRID = "discharge_grid"      # sell to grid
    DISCHARGE_HOUSE = "discharge_house"    # self-consume
    IDLE = "idle"


class SpikeStatus(Enum):
    NONE = "none"
    POTENTIAL = "potential"
    ACTUAL = "actual"


class PriceChannel(Enum):
    GENERAL = "general"
    FEED_IN = "feedIn"
    CONTROLLED_LOAD = "controlledLoad"


@dataclass
class PriceInterval:
    timestamp: datetime
    per_kwh_cents: float          # c/kWh including all charges
    spot_per_kwh_cents: float     # wholesale spot only
    channel: PriceChannel
    spike_status: SpikeStatus
    renewables_pct: float
    is_forecast: bool


@dataclass
class BatteryState:
    soc_pct: float                # 0-100
    soc_kwh: float
    capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    round_trip_efficiency: float
    cycle_cost_cents: float       # degradation cost per full cycle
    min_soc_pct: float            # reserve floor

    @property
    def usable_kwh(self) -> float:
        return max(0, self.soc_kwh - (self.capacity_kwh * self.min_soc_pct / 100))

    @property
    def headroom_kwh(self) -> float:
        return self.capacity_kwh - self.soc_kwh


@dataclass
class SolarForecast:
    timestamp: datetime
    generation_kw: float
    cloud_cover_pct: float
    temperature_c: float


@dataclass
class HouseholdLoad:
    timestamp: datetime
    predicted_kw: float


@dataclass
class GridState:
    timestamp: datetime
    nem_region: str
    demand_mw: float
    price_aud_mwh: float          # AEMO dispatch price
    renewables_pct: float
    interconnector_flow_mw: float  # positive = importing


@dataclass
class Decision:
    timestamp: datetime
    action: BatteryAction
    power_kw: float               # how much to charge/discharge
    reason: str
    confidence: float             # 0-1
    expected_value_cents: float   # expected savings/revenue for this interval
    factors: dict = field(default_factory=dict)
