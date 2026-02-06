"""Strategy engine — decides what the battery should do right now.
Tuned for 5-minute decision intervals on Endeavour Energy network.
"""
import logging
from datetime import datetime

from src.core.config import Config
from src.data.collector import Snapshot
from src.models.types import (
    BatteryAction,
    Decision,
    PriceChannel,
    PriceDescriptor,
    SpikeStatus,
)

logger = logging.getLogger(__name__)

# 5-min interval = 1/12 of an hour
INTERVAL_FRACTION = 1 / 12


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
        export_cents = abs(export_price.per_kwh_cents) if export_price else 5
        spot_cents = import_price.spot_per_kwh_cents if import_price else 15
        spike = import_price.spike_status if import_price else SpikeStatus.NONE
        descriptor = import_price.descriptor if import_price else PriceDescriptor.NEUTRAL

        # Forecast analysis — general channel only, future intervals only
        forecast_general = [
            p for p in snap.price_forecast if p.channel == PriceChannel.GENERAL
        ]
        forecast_feedin = [
            p for p in snap.price_forecast if p.channel == PriceChannel.FEED_IN
        ]

        peak_price = max((p.per_kwh_cents for p in forecast_general), default=30)
        avg_price = (
            sum(p.per_kwh_cents for p in forecast_general) / len(forecast_general)
            if forecast_general else 30
        )
        # Peak export in next few hours
        peak_export = max(
            (abs(p.per_kwh_cents) for p in forecast_feedin[:36]),  # next 3 hours
            default=5,
        )

        # Solar remaining today
        solar_remaining_kwh = sum(
            f.generation_kw
            for f in snap.solar_forecast
            if f.timestamp.date() == now.date() and f.timestamp.hour > now.hour
        )

        factors = {
            "import_cents": round(import_cents, 2),
            "export_cents": round(export_cents, 2),
            "spot_cents": round(spot_cents, 2),
            "spike": spike.value,
            "descriptor": descriptor.value,
            "tariff_period": snap.tariff_period,
            "battery_soc": battery.soc_pct,
            "solar_kw": snap.current_solar_kw,
            "load_kw": snap.predicted_load_kw,
            "peak_forecast_cents": round(peak_price, 2),
            "avg_forecast_cents": round(avg_price, 2),
            "peak_export_cents": round(peak_export, 2),
            "solar_remaining_kwh": round(solar_remaining_kwh, 2),
            "aemo_price_mwh": snap.grid_state.price_aud_mwh,
            "aemo_renewables_pct": snap.grid_state.renewables_pct,
            "vpp_active": snap.vpp_event_active,
        }

        # === Priority-ordered decision cascade ===

        # 1. VPP event — always participate for bonus revenue
        if snap.vpp_event_active and battery.usable_kwh > 0:
            return self._decision(now, BatteryAction.DISCHARGE_GRID,
                battery.max_discharge_kw,
                "VPP event active — max discharge for bonus revenue",
                0.95, export_cents * battery.max_discharge_kw * INTERVAL_FRACTION,
                factors)

        # 2. Actual spike — avoid extreme grid costs
        if spike == SpikeStatus.ACTUAL and battery.usable_kwh > 0:
            power = min(snap.predicted_load_kw, battery.max_discharge_kw)
            return self._decision(now, BatteryAction.DISCHARGE_HOUSE, power,
                f"SPIKE ACTIVE ({import_cents:.0f}c) — battery powering house",
                0.99, import_cents * power * INTERVAL_FRACTION, factors)

        # 3. Potential spike — build reserve
        if spike == SpikeStatus.POTENTIAL and battery.soc_pct < self._config.spike_reserve_soc_pct:
            return self._decision(now, BatteryAction.CHARGE_GRID,
                battery.max_charge_kw,
                f"Potential spike — charging to {self._config.spike_reserve_soc_pct}% reserve",
                0.7, 0, factors)

        # 4. Negative price — get paid to charge
        if import_cents <= 0 and battery.headroom_kwh > 0:
            return self._decision(now, BatteryAction.CHARGE_GRID,
                battery.max_charge_kw,
                f"NEGATIVE price ({import_cents:.1f}c) — paid to charge",
                0.99, abs(import_cents) * battery.max_charge_kw * INTERVAL_FRACTION,
                factors)

        # 5. Extremely low / very low price — arbitrage charge
        if descriptor in (PriceDescriptor.EXTREMELY_LOW, PriceDescriptor.VERY_LOW):
            if battery.headroom_kwh > 0:
                effective_buy = import_cents / battery.round_trip_efficiency
                cycle_cost = battery.cycle_cost_cents * battery.max_charge_kw * INTERVAL_FRACTION / battery.capacity_kwh
                margin = peak_price - effective_buy - cycle_cost
                if margin > 5:
                    return self._decision(now, BatteryAction.CHARGE_GRID,
                        battery.max_charge_kw,
                        f"Low price ({import_cents:.1f}c, {descriptor.value}) — "
                        f"arbitrage margin {margin:.1f}c to peak {peak_price:.0f}c",
                        0.8, margin * battery.max_charge_kw * INTERVAL_FRACTION,
                        factors)

        # 6. Cheap grid below threshold — charge if good arbitrage
        if import_cents < self._config.charge_price_threshold_cents and battery.headroom_kwh > 0:
            effective_buy = import_cents / battery.round_trip_efficiency
            cycle_cost = battery.cycle_cost_cents * battery.max_charge_kw * INTERVAL_FRACTION / battery.capacity_kwh
            margin = peak_price - effective_buy - cycle_cost
            if margin > 8:
                return self._decision(now, BatteryAction.CHARGE_GRID,
                    battery.max_charge_kw,
                    f"Below threshold ({import_cents:.1f}c < {self._config.charge_price_threshold_cents}c) — "
                    f"margin {margin:.1f}c",
                    0.75, margin * battery.max_charge_kw * INTERVAL_FRACTION,
                    factors)

        # 7. High export price — sell to grid
        if export_cents > self._config.sell_price_threshold_cents and battery.usable_kwh > 0:
            # Don't sell if significantly higher export coming in next 3 hours
            future_higher = any(
                abs(p.per_kwh_cents) > export_cents * 1.3
                for p in forecast_feedin[:36]
            )
            if not future_higher:
                return self._decision(now, BatteryAction.DISCHARGE_GRID,
                    battery.max_discharge_kw,
                    f"High export ({export_cents:.1f}c, descriptor={descriptor.value}) — selling",
                    0.85, export_cents * battery.max_discharge_kw * INTERVAL_FRACTION,
                    factors)

        # 8. Solar excess — store in battery
        solar_excess = snap.current_solar_kw - snap.predicted_load_kw
        if solar_excess > 0.3 and battery.headroom_kwh > 0:
            charge_kw = min(solar_excess, battery.max_charge_kw)
            return self._decision(now, BatteryAction.CHARGE_SOLAR, charge_kw,
                f"Solar excess ({solar_excess:.1f}kW) — storing",
                0.9, avg_price * charge_kw * INTERVAL_FRACTION, factors)

        # 9. Self-consume during peak tariff or high prices
        if snap.tariff_period == "peak" or import_cents > avg_price * 1.2:
            if battery.usable_kwh > 0:
                power = min(snap.predicted_load_kw, battery.max_discharge_kw)
                savings = import_cents * power * INTERVAL_FRACTION
                degradation = battery.cycle_cost_cents * power * INTERVAL_FRACTION / battery.capacity_kwh
                if savings > degradation:
                    return self._decision(now, BatteryAction.DISCHARGE_HOUSE, power,
                        f"Self-consume — {snap.tariff_period} tariff, {import_cents:.1f}c "
                        f"(avg {avg_price:.0f}c)",
                        0.7, savings - degradation, factors)

        # 10. Idle
        return self._decision(now, BatteryAction.IDLE, 0,
            f"No action — {import_cents:.1f}c import, {export_cents:.1f}c export, "
            f"SOC {battery.soc_pct:.0f}%, {descriptor.value}",
            0.6, 0, factors)

    def _decision(self, ts, action, power, reason, confidence, value, factors):
        return Decision(
            timestamp=ts, action=action, power_kw=power,
            reason=reason, confidence=confidence,
            expected_value_cents=round(value, 2), factors=factors,
        )
