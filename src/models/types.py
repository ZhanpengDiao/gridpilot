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


class PriceDescriptor(Enum):
    """Amber's price descriptor — indicates how current price compares to typical."""
    NEGATIVE = "negative"
    EXTREMELY_LOW = "extremelyLow"
    VERY_LOW = "veryLow"
    LOW = "low"
    NEUTRAL = "neutral"
    HIGH = "high"
    SPIKE = "spike"


class TariffPeriod(Enum):
    OFF_PEAK = "offPeak"
    SHOULDER = "shoulder"
    PEAK = "peak"


class TariffSeason(Enum):
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"
    SPRING = "spring"


@dataclass
class TariffInfo:
    period: TariffPeriod
    season: TariffSeason


@dataclass
class PriceInterval:
    timestamp: datetime
    end_time: datetime
    per_kwh_cents: float          # c/kWh including all charges
    spot_per_kwh_cents: float     # wholesale spot only
    channel: PriceChannel
    spike_status: SpikeStatus
    descriptor: PriceDescriptor
    renewables_pct: float
    tariff: TariffInfo | None
    duration_minutes: int         # 5 for your site
    interval_type: str            # ActualInterval, CurrentInterval, ForecastInterval
    is_estimate: bool

    @property
    def is_forecast(self) -> bool:
        return self.interval_type == "ForecastInterval"

    @property
    def is_current(self) -> bool:
        return self.interval_type == "CurrentInterval"


@dataclass
class UsageInterval:
    timestamp: datetime
    end_time: datetime
    channel: PriceChannel
    channel_id: str               # E1, B1
    kwh: float
    cost_cents: float
    per_kwh_cents: float
    spot_per_kwh_cents: float
    spike_status: SpikeStatus
    descriptor: PriceDescriptor
    renewables_pct: float
    tariff: TariffInfo | None
    quality: str                  # billable, estimated


@dataclass
class SiteInfo:
    site_id: str
    nmi: str
    network: str
    status: str
    active_from: str
    interval_minutes: int
    channels: list[dict]

    @property
    def has_feed_in(self) -> bool:
        return any(c["type"] == "feedIn" for c in self.channels)

    @property
    def has_battery(self) -> bool:
        return any(c["type"] == "battery" for c in self.channels)

    @property
    def channel_ids(self) -> dict[str, str]:
        return {c["type"]: c["identifier"] for c in self.channels}


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
