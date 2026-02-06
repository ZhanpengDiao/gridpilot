import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Amber
    amber_api_token: str = os.getenv("AMBER_API_TOKEN", "")
    amber_site_id: str = os.getenv("AMBER_SITE_ID", "")

    # Battery
    battery_type: str = os.getenv("BATTERY_TYPE", "generic")
    battery_capacity_kwh: float = float(os.getenv("BATTERY_CAPACITY_KWH", "13.5"))
    battery_max_charge_kw: float = float(os.getenv("BATTERY_MAX_CHARGE_KW", "5.0"))
    battery_max_discharge_kw: float = float(os.getenv("BATTERY_MAX_DISCHARGE_KW", "5.0"))
    battery_round_trip_efficiency: float = float(os.getenv("BATTERY_ROUND_TRIP_EFFICIENCY", "0.9"))
    battery_min_soc_pct: float = float(os.getenv("BATTERY_MIN_SOC_PERCENT", "20"))
    battery_cycle_cost_cents: float = float(os.getenv("BATTERY_CYCLE_COST_CENTS", "5"))

    # Location
    latitude: float = float(os.getenv("LATITUDE", "-33.8688"))
    longitude: float = float(os.getenv("LONGITUDE", "151.2093"))
    nem_region: str = os.getenv("NEM_REGION", "NSW1")

    # Strategy thresholds
    charge_price_threshold_cents: float = float(os.getenv("CHARGE_PRICE_THRESHOLD_CENTS", "8"))
    sell_price_threshold_cents: float = float(os.getenv("SELL_PRICE_THRESHOLD_CENTS", "25"))
    spike_reserve_soc_pct: float = float(os.getenv("SPIKE_RESERVE_SOC_PERCENT", "40"))

    # Engine
    decision_interval_seconds: int = int(os.getenv("DECISION_INTERVAL_SECONDS", "300"))

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
