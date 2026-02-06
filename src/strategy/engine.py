"""Strategy engine — decides what the battery should do right now."""
import logging
from datetime import datetime, timedelta

from src.core.config import Config
from src.data.collector import Snapshot
from src.models.types import (
    BatteryAction,
    Decision,
    PriceChannel,
    SpikeStatus,
)

logger = logging.getLogger(__name__)


class StrategyEngine:
    def __init__(self, config: Config):
        self._config = config

    def decide(self, snap: Snapshot) -> Decision:
        """Evaluate all factors and return the optimal battery action."""
        now = snap.timestamp
        battery = snap.battery
        import_price = snap.current_import_price
        export_price = snap.current_export_price

        import_cents = import_price.per_kwh_cents if import_price else 30
        export_cents = export_price.per_kwh_cents if export_price else 5
        spike = import_price.spike_status if import_price else SpikeStatus.NONE

        # Forecast analysis
        forecast_import = [
            p for p in snap.price_forecast if p.channel == PriceChannel.GENERAL
        ]
        peak_price = max((p.per_kwh_cents for p in forecast_import), default=30)
        avg_price = (
            sum(p.per_kwh_cents for p in forecast_import) / len(forecast_import)
            if forecast_import else 30
        )

        # Hours of solar remaining today
        solar_remaining_kwh = sum(
            f.generation_kw
            for f in snap.solar_forecast
            if f.timestamp.date() == now.date() and f.timestamp.hour > now.hour
        )

        factors = {
            "import_cents": import_cents,
            "export_cents": export_cents,
            "spike": spike.value,
            "battery_soc": battery.soc_pct,
            "solar_kw": snap.current_solar_kw,
            "load_kw": snap.predicted_load_kw,
            "peak_forecast_cents": peak_price,
            "avg_forecast_cents": avg_price,
            "solar_remaining_kwh": solar_remaining_kwh,
            "aemo_price_mwh": snap.grid_state.price_aud_mwh,
            "vpp_active": snap.vpp_event_active,
        }

        # === Priority-ordered decision cascade ===

        # 1. VPP event — always participate for bonus revenue
        if snap.vpp_event_active and battery.usable_kwh > 0:
            return Decision(
                timestamp=now,
                action=BatteryAction.DISCHARGE_GRID,
                power_kw=battery.max_discharge_kw,
                reason="VPP event active — max discharge for bonus revenue",
                confidence=0.95,
                expected_value_cents=export_cents * battery.max_discharge_kw / 12,  # 5-min interval
                factors=factors,
            )

        # 2. Spike protection — avoid extreme grid costs
        if spike == SpikeStatus.ACTUAL and battery.usable_kwh > 0:
            return Decision(
                timestamp=now,
                action=BatteryAction.DISCHARGE_HOUSE,
                power_kw=min(snap.predicted_load_kw, battery.max_discharge_kw),
                reason=f"Price spike ACTIVE ({import_cents:.0f}c) — using battery to avoid grid",
                confidence=0.99,
                expected_value_cents=import_cents * snap.predicted_load_kw / 12,
                factors=factors,
            )

        # 3. Potential spike — reserve battery, stop selling
        if spike == SpikeStatus.POTENTIAL:
            if battery.soc_pct < self._config.spike_reserve_soc_pct:
                return Decision(
                    timestamp=now,
                    action=BatteryAction.CHARGE_GRID,
                    power_kw=battery.max_charge_kw,
                    reason=f"Potential spike — pre-charging to {self._config.spike_reserve_soc_pct}% reserve",
                    confidence=0.7,
                    expected_value_cents=0,
                    factors=factors,
                )

        # 4. Negative/very cheap price — charge from grid (get paid!)
        if import_cents <= 0:
            if battery.headroom_kwh > 0:
                return Decision(
                    timestamp=now,
                    action=BatteryAction.CHARGE_GRID,
                    power_kw=battery.max_charge_kw,
                    reason=f"Negative price ({import_cents:.1f}c) — getting paid to charge",
                    confidence=0.99,
                    expected_value_cents=abs(import_cents) * battery.max_charge_kw / 12,
                    factors=factors,
                )

        # 5. Cheap grid price — charge if profitable to arbitrage later
        if import_cents < self._config.charge_price_threshold_cents and battery.headroom_kwh > 0:
            # Only worth it if we can sell later at a profit after efficiency loss + degradation
            effective_buy = import_cents / battery.round_trip_efficiency
            cycle_cost = battery.cycle_cost_cents * (battery.max_charge_kw / 12) / battery.capacity_kwh
            profit_potential = peak_price - effective_buy - cycle_cost
            if profit_potential > 5:  # at least 5c/kWh margin
                return Decision(
                    timestamp=now,
                    action=BatteryAction.CHARGE_GRID,
                    power_kw=battery.max_charge_kw,
                    reason=f"Cheap grid ({import_cents:.1f}c) — arbitrage potential {profit_potential:.1f}c margin to peak {peak_price:.0f}c",
                    confidence=0.8,
                    expected_value_cents=profit_potential * battery.max_charge_kw / 12,
                    factors=factors,
                )

        # 6. High export price — sell if profitable and future prices are lower
        if export_cents > self._config.sell_price_threshold_cents and battery.usable_kwh > 0:
            # Check if price is near peak — don't sell too early
            future_higher = any(
                p.per_kwh_cents > export_cents * 1.2
                for p in forecast_import[:6]  # next 3 hours
            )
            if not future_higher:
                return Decision(
                    timestamp=now,
                    action=BatteryAction.DISCHARGE_GRID,
                    power_kw=battery.max_discharge_kw,
                    reason=f"High export price ({export_cents:.1f}c) — selling to grid",
                    confidence=0.85,
                    expected_value_cents=export_cents * battery.max_discharge_kw / 12,
                    factors=factors,
                )

        # 7. Solar excess — charge battery with free solar
        solar_excess = snap.current_solar_kw - snap.predicted_load_kw
        if solar_excess > 0.5 and battery.headroom_kwh > 0:
            charge_kw = min(solar_excess, battery.max_charge_kw)
            return Decision(
                timestamp=now,
                action=BatteryAction.CHARGE_SOLAR,
                power_kw=charge_kw,
                reason=f"Solar excess ({solar_excess:.1f}kW) — storing for later",
                confidence=0.9,
                expected_value_cents=avg_price * charge_kw / 12,
                factors=factors,
            )

        # 8. Self-consume — use battery if grid price is above average
        if import_cents > avg_price and battery.usable_kwh > 0:
            discharge_kw = min(snap.predicted_load_kw, battery.max_discharge_kw)
            # Only if savings exceed degradation cost
            savings = import_cents * discharge_kw / 12
            degradation = battery.cycle_cost_cents * discharge_kw / 12 / battery.capacity_kwh
            if savings > degradation:
                return Decision(
                    timestamp=now,
                    action=BatteryAction.DISCHARGE_HOUSE,
                    power_kw=discharge_kw,
                    reason=f"Self-consume — grid at {import_cents:.1f}c (above avg {avg_price:.0f}c)",
                    confidence=0.7,
                    expected_value_cents=savings - degradation,
                    factors=factors,
                )

        # 9. Default — idle
        return Decision(
            timestamp=now,
            action=BatteryAction.IDLE,
            power_kw=0,
            reason=f"No profitable action — import {import_cents:.1f}c, export {export_cents:.1f}c, SOC {battery.soc_pct:.0f}%",
            confidence=0.6,
            expected_value_cents=0,
            factors=factors,
        )
