"""Day-ahead strategy planner.

Looks at the full 48h price forecast, learned usage profile, and solar forecast
to build an optimal charge/discharge schedule. Runs once per 30-min Amber interval
(or on significant price change). The 5-min loop follows the plan unless a real-time
override is needed (spike, VPP, negative price).
"""
import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ScheduledAction:
    start_time: str       # HH:MM
    end_time: str         # HH:MM
    action: str           # charge_grid, sell_grid, self_consume, charge_solar, idle
    reason: str
    import_price: float   # expected c/kWh during this window
    export_price: float
    expected_value: float # cents saved/earned in this window
    priority: int         # for display ordering


@dataclass
class DayPlan:
    created_at: str
    schedule: list[ScheduledAction]
    summary: dict  # totals

    def action_for_time(self, hour: int, minute: int) -> ScheduledAction | None:
        t = f"{hour:02d}:{minute:02d}"
        for s in self.schedule:
            if s.start_time <= t < s.end_time:
                return s
        return None


def build_day_plan(
    forecast_general: list[dict],
    forecast_feedin: list[dict],
    solar_forecast: list[dict],  # hourly from weather
    profile: dict | None,        # learned usage profile
    config: dict,                # thresholds + battery specs
) -> DayPlan:
    """Build optimal schedule from all available data."""

    battery_kwh = config.get("battery_capacity_kwh", 13.5)
    efficiency = config.get("round_trip_efficiency", 0.9)
    cycle_cost = config.get("cycle_cost_cents", 5) / battery_kwh
    max_charge_kw = config.get("max_charge_kw", 5.0)
    max_discharge_kw = config.get("max_discharge_kw", 5.0)
    min_soc_pct = config.get("min_soc_pct", 20)
    usable_kwh = battery_kwh * (1 - min_soc_pct / 100)

    # ── Step 1: Build time-indexed price map ──────────────────
    # Group forecast into 30-min windows with import + export prices
    windows = _build_windows(forecast_general, forecast_feedin)
    if not windows:
        return _empty_plan()

    # ── Step 2: Overlay solar + load predictions ──────────────
    for w in windows:
        h = w["hour"]
        # Solar from weather forecast
        w["solar_kw"] = _solar_for_hour(solar_forecast, h)
        # Load from learned profile
        is_weekday = datetime.now().weekday() < 5
        if profile:
            w["load_kw"] = profile.get("hours", [{}] * 24)[h].get(
                "weekday_import_kw" if is_weekday else "weekend_import_kw", 0.3)
            w["expected_export_kw"] = profile.get("hours", [{}] * 24)[h].get(
                "weekday_export_kw" if is_weekday else "weekend_export_kw", 0)
        else:
            w["load_kw"] = 0.3
            w["expected_export_kw"] = 0
        w["net_kw"] = w["load_kw"] - w["solar_kw"]  # positive = need grid, negative = excess solar

    # ── Step 3: Find optimal charge windows ───────────────────
    # Prefer offPeak windows for charging — add tariff penalty for peak/shoulder
    TARIFF_CHARGE_PENALTY = {"offPeak": 0, "shoulder": 3, "peak": 10}  # extra c/kWh
    charge_candidates = [
        w for w in windows
        if w["import_cents"] > 0  # negative handled separately as override
    ]
    charge_candidates.sort(
        key=lambda w: w["import_cents"] / efficiency + cycle_cost + TARIFF_CHARGE_PENALTY.get(w.get("tariff", "offPeak"), 0)
    )

    # ── Step 4: Find optimal sell windows ─────────────────────
    sell_candidates = [w for w in windows if w["export_cents"] > 0]
    sell_candidates.sort(key=lambda w: w["export_cents"], reverse=True)

    # ── Step 5: Match charge→sell pairs for profitable arbitrage ─
    schedule = []
    remaining_capacity = usable_kwh
    total_charge_value = 0
    total_sell_value = 0
    charged_windows = set()
    sold_windows = set()

    for sell_w in sell_candidates:
        if remaining_capacity <= 0:
            break
        for charge_w in charge_candidates:
            if charge_w["key"] in charged_windows:
                continue
            if charge_w["time_idx"] >= sell_w["time_idx"]:
                continue  # can't charge after selling

            # Profit calculation
            buy_cost = charge_w["import_cents"] / efficiency + cycle_cost
            sell_revenue = sell_w["export_cents"]
            margin = sell_revenue - buy_cost

            if margin < 5:  # minimum 5c/kWh margin
                continue

            # How much can we charge/sell in this 30-min window?
            window_kwh = min(max_charge_kw * 0.5, remaining_capacity)  # 30min at max rate

            # Schedule charge
            if charge_w["key"] not in charged_windows:
                schedule.append(ScheduledAction(
                    start_time=charge_w["start"],
                    end_time=charge_w["end"],
                    action="charge_grid",
                    reason=f"Buy at {charge_w['import_cents']:.1f}c → sell at {sell_w['export_cents']:.1f}c (margin {margin:.1f}c)",
                    import_price=charge_w["import_cents"],
                    export_price=0,
                    expected_value=margin * window_kwh,
                    priority=1,
                ))
                charged_windows.add(charge_w["key"])
                total_charge_value += charge_w["import_cents"] * window_kwh

            # Schedule sell
            if sell_w["key"] not in sold_windows:
                schedule.append(ScheduledAction(
                    start_time=sell_w["start"],
                    end_time=sell_w["end"],
                    action="sell_grid",
                    reason=f"Sell at {sell_w['export_cents']:.1f}c (bought at ~{charge_w['import_cents']:.0f}c)",
                    import_price=0,
                    export_price=sell_w["export_cents"],
                    expected_value=sell_w["export_cents"] * window_kwh,
                    priority=1,
                ))
                sold_windows.add(sell_w["key"])
                total_sell_value += sell_w["export_cents"] * window_kwh

            remaining_capacity -= window_kwh
            break  # move to next sell window

    # ── Step 6: Self-consume during expensive / peak tariff windows ─
    # Peak tariff windows get priority for self-consumption even at median price
    TARIFF_SELF_CONSUME_BONUS = {"peak": 15, "shoulder": 5, "offPeak": 0}  # virtual c/kWh bonus
    median_price = _median([x["import_cents"] for x in windows])

    self_consume_candidates = [
        w for w in windows
        if w["key"] not in sold_windows and w["key"] not in charged_windows and w["net_kw"] > 0
    ]
    # Sort by value of self-consuming: import price + tariff bonus
    self_consume_candidates.sort(
        key=lambda w: w["import_cents"] + TARIFF_SELF_CONSUME_BONUS.get(w.get("tariff", "offPeak"), 0),
        reverse=True,
    )

    for w in self_consume_candidates:
        effective_value = w["import_cents"] + TARIFF_SELF_CONSUME_BONUS.get(w.get("tariff", "offPeak"), 0)
        # Self-consume if: peak/shoulder tariff, OR above-median price, OR spike risk
        if w.get("tariff") in ("peak", "shoulder") or w["import_cents"] > median_price or w.get("spike_risk"):
            tariff_label = f" [{w.get('tariff', '?')}]" if w.get("tariff") != "offPeak" else ""
            spike_label = " ⚠️spike risk" if w.get("spike_risk") else ""
            schedule.append(ScheduledAction(
                start_time=w["start"],
                end_time=w["end"],
                action="self_consume",
                reason=f"{w['import_cents']:.1f}c{tariff_label}{spike_label} — use battery for {w['load_kw']:.1f}kW load",
                import_price=w["import_cents"],
                export_price=0,
                expected_value=w["import_cents"] * min(w["load_kw"], max_discharge_kw) * 0.5,
                priority=2,
            ))

    # ── Step 7: Solar charge during excess solar windows ──────
    for w in windows:
        if w["key"] in charged_windows:
            continue
        if w["solar_kw"] > w["load_kw"] + 0.3:
            excess = w["solar_kw"] - w["load_kw"]
            schedule.append(ScheduledAction(
                start_time=w["start"],
                end_time=w["end"],
                action="charge_solar",
                reason=f"Solar excess {excess:.1f}kW — store for later",
                import_price=0,
                export_price=w["export_cents"],
                expected_value=w["export_cents"] * min(excess, max_charge_kw) * 0.5,
                priority=3,
            ))

    # Sort by time
    schedule.sort(key=lambda s: s.start_time)

    # Summary
    total_expected = sum(s.expected_value for s in schedule)
    arbitrage_pairs = len(charged_windows)

    return DayPlan(
        created_at=datetime.now().isoformat(),
        schedule=schedule,
        summary={
            "arbitrage_pairs": arbitrage_pairs,
            "total_expected_cents": round(total_expected, 1),
            "charge_windows": len(charged_windows),
            "sell_windows": len(sold_windows),
            "self_consume_windows": sum(1 for s in schedule if s.action == "self_consume"),
            "solar_charge_windows": sum(1 for s in schedule if s.action == "charge_solar"),
        },
    )


def format_plan(plan: DayPlan) -> str:
    """Format day plan for display."""
    lines = [
        f"  📋 DAY PLAN (created {plan.created_at[11:16]})",
        f"     Arbitrage pairs: {plan.summary['arbitrage_pairs']} | "
        f"Expected value: {plan.summary['total_expected_cents']:.0f}c "
        f"(${plan.summary['total_expected_cents']/100:.2f})",
        f"     Self-consume: {plan.summary['self_consume_windows']} windows "
        f"(peak/shoulder prioritised) | Solar charge: {plan.summary['solar_charge_windows']}",
        "",
        f"     {'Time':12s} {'Action':16s} {'Price':>8s}  Reason",
        f"     {'─'*70}",
    ]
    for s in plan.schedule:
        price = f"{s.import_price:.0f}c" if s.import_price else f"{s.export_price:.0f}c"
        icon = {"charge_grid": "⚡", "sell_grid": "💰", "self_consume": "🏠", "charge_solar": "☀️"}.get(s.action, "·")
        lines.append(f"     {s.start_time}-{s.end_time}  {icon} {s.action:14s} {price:>8s}  {s.reason}")
    return "\n".join(lines)


def should_override(plan: DayPlan, current_import: float, current_export: float, spike: str) -> tuple[bool, str, str]:
    """Check if real-time conditions warrant overriding the plan."""
    if spike == "actual":
        return True, "discharge_house", f"SPIKE OVERRIDE ({current_import:.0f}c) — protect house"
    if spike == "potential":
        return True, "charge_grid", "SPIKE WARNING — building reserve"
    if current_import <= 0:
        return True, "charge_grid", f"NEGATIVE PRICE OVERRIDE ({current_import:.1f}c) — free energy"
    if current_export > 500:  # extreme export price
        return True, "sell_grid", f"EXTREME EXPORT OVERRIDE ({current_export:.0f}c) — sell everything"
    return False, "", ""


# ── Helpers ───────────────────────────────────────────────────

def _build_windows(general: list[dict], feedin: list[dict]) -> list[dict]:
    """Group 5-min intervals into 30-min windows with avg prices and tariff info."""
    from collections import defaultdict

    gen_by_slot = defaultdict(list)
    fi_by_slot = defaultdict(list)
    tariff_by_slot = {}
    spike_by_slot = {}

    for p in general:
        if p.get("type") != "ForecastInterval":
            continue
        t = p.get("nemTime", p.get("startTime", ""))
        if len(t) >= 16:
            h = int(t[11:13])
            m = int(t[14:16])
            slot = f"{h:02d}:{(m // 30) * 30:02d}"
            gen_by_slot[slot].append(p["perKwh"])
            # Capture tariff info from first interval in slot
            if slot not in tariff_by_slot:
                ti = p.get("tariffInformation", {})
                tariff_by_slot[slot] = ti.get("period", "offPeak")
            if p.get("spikeStatus", "none") != "none":
                spike_by_slot[slot] = p["spikeStatus"]

    for p in feedin:
        if p.get("type") != "ForecastInterval":
            continue
        t = p.get("nemTime", p.get("startTime", ""))
        if len(t) >= 16:
            h = int(t[11:13])
            m = int(t[14:16])
            slot = f"{h:02d}:{(m // 30) * 30:02d}"
            fi_by_slot[slot].append(abs(p["perKwh"]))

    windows = []
    for i, slot in enumerate(sorted(gen_by_slot.keys())):
        h, m = int(slot[:2]), int(slot[3:5])
        end_m = m + 30
        end_h = h + (end_m // 60)
        end_m = end_m % 60
        tariff = tariff_by_slot.get(slot, "offPeak")
        windows.append({
            "key": slot,
            "time_idx": i,
            "hour": h,
            "start": slot,
            "end": f"{end_h:02d}:{end_m:02d}",
            "import_cents": sum(gen_by_slot[slot]) / len(gen_by_slot[slot]),
            "export_cents": sum(fi_by_slot.get(slot, [0])) / max(len(fi_by_slot.get(slot, [1])), 1),
            "tariff": tariff,
            "spike_risk": slot in spike_by_slot,
        })
    return windows


def _solar_for_hour(solar_forecast: list[dict], hour: int) -> float:
    if not solar_forecast:
        return 0
    times = solar_forecast.get("time", []) if isinstance(solar_forecast, dict) else []
    radiation = solar_forecast.get("direct_radiation", []) if isinstance(solar_forecast, dict) else []
    for i, t in enumerate(times):
        if len(t) >= 13 and int(t[11:13]) == hour:
            irr = radiation[i] if i < len(radiation) else 0
            return round((irr or 0) * 20 / 1000 * 0.15, 2)
    return 0


def _median(values: list[float]) -> float:
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _empty_plan() -> DayPlan:
    return DayPlan(
        created_at=datetime.now().isoformat(),
        schedule=[],
        summary={"arbitrage_pairs": 0, "total_expected_cents": 0,
                 "charge_windows": 0, "sell_windows": 0,
                 "self_consume_windows": 0, "solar_charge_windows": 0},
    )
