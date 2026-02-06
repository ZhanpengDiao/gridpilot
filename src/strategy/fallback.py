"""Fallback strategy when data sources are unavailable."""
import logging
from datetime import datetime

from src.core.config import Config
from src.models.types import BatteryAction, BatteryState, Decision

logger = logging.getLogger(__name__)


class FallbackStrategy:
    """Conservative rules when we can't reach APIs — protect the house."""

    def __init__(self, config: Config):
        self._config = config

    def decide(self, battery: BatteryState) -> Decision:
        hour = datetime.now().hour

        # Evening peak (4-9pm) — always discharge for house
        if 16 <= hour < 21 and battery.usable_kwh > 0:
            return Decision(
                timestamp=datetime.now(),
                action=BatteryAction.DISCHARGE_HOUSE,
                power_kw=battery.max_discharge_kw,
                reason="FALLBACK: evening peak — self-consume",
                confidence=0.5,
                expected_value_cents=0,
                factors={"mode": "fallback", "reason": "no_data"},
            )

        # Daytime (9am-4pm) — assume solar, charge battery
        if 9 <= hour < 16 and battery.headroom_kwh > 0:
            return Decision(
                timestamp=datetime.now(),
                action=BatteryAction.CHARGE_SOLAR,
                power_kw=battery.max_charge_kw * 0.5,
                reason="FALLBACK: daytime — assume solar available",
                confidence=0.3,
                expected_value_cents=0,
                factors={"mode": "fallback", "reason": "no_data"},
            )

        # Otherwise — idle, preserve battery
        return Decision(
            timestamp=datetime.now(),
            action=BatteryAction.IDLE,
            power_kw=0,
            reason="FALLBACK: no data, preserving battery",
            confidence=0.3,
            expected_value_cents=0,
            factors={"mode": "fallback", "reason": "no_data"},
        )
