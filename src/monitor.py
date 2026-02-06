"""GridPilot dry-run monitor — fetches live data, prints dashboard, gives recommendations."""
import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta

import httpx

from src.core.config import Config
from src.strategy.planner import build_day_plan, format_plan, should_override, DayPlan

LOG_FILE = "data/gridpilot.log"

import os
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
logger = logging.getLogger("gridpilot")

AMBER_BASE = "https://api.amber.com.au/v1"
WEATHER_BASE = "https://api.open-meteo.com/v1"
AEMO_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY"

# 5-min interval = 1/12 hour
INTERVAL_H = 1 / 12


async def fetch_amber(http: httpx.AsyncClient, token: str, site_id: str):
    headers = {"Authorization": f"Bearer {token}"}
    current = await http_retry(http, f"{AMBER_BASE}/sites/{site_id}/prices/current", headers=headers)
    await asyncio.sleep(1)  # rate limit
    forecast = await http_retry(http, f"{AMBER_BASE}/sites/{site_id}/prices", headers=headers, params={"next": 48})
    return current, forecast


async def http_retry(http: httpx.AsyncClient, url: str, deadline_seconds: float = 270, backoff: float = 5, **kwargs) -> list:
    """Keep retrying until deadline (default 4.5min of the 5min cycle). Exponential backoff capped at 30s."""
    import time
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        elapsed = time.monotonic() - start
        if elapsed > deadline_seconds:
            logger.error("Deadline exceeded for %s after %d attempts (%.0fs)", url.split("/")[-1], attempt - 1, elapsed)
            return []
        try:
            resp = await http.get(url, **kwargs)
            if resp.status_code == 429:
                wait = min(backoff * attempt, 30)
                logger.warning("Rate limited on %s — retry in %.0fs (attempt %d)", url.split("/")[-1], wait, attempt)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            remaining = deadline_seconds - (time.monotonic() - start)
            if remaining <= 0:
                logger.error("Deadline exceeded for %s: %s", url.split("/")[-1], e)
                return []
            wait = min(backoff * attempt, 30, remaining)
            logger.warning("Attempt %d for %s failed: %s — retry in %.0fs (%.0fs remaining)",
                           attempt, url.split("/")[-1], e, wait, remaining)
            await asyncio.sleep(wait)


async def fetch_weather(http: httpx.AsyncClient, lat: float, lon: float):
    data = await http_retry(http, f"{WEATHER_BASE}/forecast", deadline_seconds=30, params={
        "latitude": lat, "longitude": lon,
        "hourly": "direct_radiation,cloud_cover,temperature_2m",
        "forecast_hours": 24, "timezone": "auto",
    })
    return data.get("hourly", {}) if isinstance(data, dict) else {}


async def fetch_aemo(http: httpx.AsyncClient, region: str):
    data = await http_retry(http, AEMO_URL, deadline_seconds=30)
    if isinstance(data, list):
        for entry in data:
            if entry.get("REGIONID") == region:
                return entry
    return {}


def analyse_forecast(all_prices: list[dict]) -> dict:
    """GridPilot's own price analysis — not Amber's descriptors."""
    general = [p for p in all_prices if p.get("channelType") == "general"]
    feedin = [p for p in all_prices if p.get("channelType") == "feedIn"]

    forecast_gen = [p for p in general if p.get("type") == "ForecastInterval"]
    forecast_fi = [p for p in feedin if p.get("type") == "ForecastInterval"]
    actual_gen = [p for p in general if p.get("type") == "ActualInterval"]

    # Price stats from forecast
    if forecast_gen:
        prices = [p["perKwh"] for p in forecast_gen]
        fi_prices = [abs(p["perKwh"]) for p in forecast_fi] if forecast_fi else [0]

        # Find cheapest and most expensive windows
        sorted_by_price = sorted(forecast_gen, key=lambda p: p["perKwh"])
        cheapest_5 = sorted_by_price[:5]
        expensive_5 = sorted_by_price[-5:]

        # Find best sell windows
        sorted_fi = sorted(forecast_fi, key=lambda p: abs(p["perKwh"]), reverse=True)
        best_sell_5 = sorted_fi[:5]
    else:
        prices = [30]
        fi_prices = [5]
        cheapest_5 = []
        expensive_5 = []
        best_sell_5 = []

    # Today's actual cost so far
    today_cost = sum(p.get("perKwh", 0) for p in actual_gen) / max(len(actual_gen), 1)

    return {
        "forecast_min": min(prices),
        "forecast_max": max(prices),
        "forecast_avg": sum(prices) / len(prices),
        "export_max": max(fi_prices),
        "export_avg": sum(fi_prices) / len(fi_prices),
        "cheapest_windows": cheapest_5,
        "expensive_windows": expensive_5,
        "best_sell_windows": best_sell_5,
        "today_avg_import": today_cost,
        "negative_intervals": sum(1 for p in prices if p <= 0),
        "spike_intervals": sum(1 for p in forecast_gen if p.get("spikeStatus") != "none"),
    }


def estimate_solar(weather: dict, hour: int) -> float:
    """Estimate current solar generation from weather data."""
    if hour < len(weather.get("direct_radiation", [])):
        irradiance = weather["direct_radiation"][hour] or 0
        return round(irradiance * 20 / 1000 * 0.15, 2)  # ~6.6kW system estimate
    return 0


def gridpilot_recommendation(
    import_cents: float,
    export_cents: float,
    analysis: dict,
    solar_kw: float,
    hour: int,
    config: Config,
    tariff_period: str = "offPeak",
) -> tuple[str, str, float]:
    """GridPilot's own recommendation. Returns (action, reason, confidence)."""

    efficiency = config.battery_round_trip_efficiency
    cycle_cost = config.battery_cycle_cost_cents / config.battery_capacity_kwh
    is_peak = tariff_period == "peak"
    is_shoulder = tariff_period == "shoulder"

    # Effective cost to store and retrieve 1 kWh
    storage_cost = import_cents / efficiency + cycle_cost

    peak_price = analysis["forecast_max"]
    avg_price = analysis["forecast_avg"]
    best_export = analysis["export_max"]

    # 1. Negative price — no brainer
    if import_cents <= 0:
        profit = abs(import_cents) + (peak_price * efficiency - cycle_cost)
        return "⚡ CHARGE FROM GRID", f"Negative price! Earn {abs(import_cents):.1f}c/kWh charging + sell later at ~{peak_price:.0f}c", 0.99

    # 2. Very cheap — arbitrage opportunity
    if import_cents < config.charge_price_threshold_cents:
        margin = peak_price - storage_cost
        if margin > 8:
            return "⚡ CHARGE FROM GRID", f"Cheap ({import_cents:.1f}c) → store → sell at peak ({peak_price:.0f}c). Margin: {margin:.1f}c/kWh after losses", 0.85
        elif margin > 3:
            return "⚡ CHARGE FROM GRID", f"Moderate arbitrage ({margin:.1f}c margin). Worth charging if battery has room", 0.6

    # 3. High export — sell
    if export_cents > config.sell_price_threshold_cents:
        # Check if even higher export coming
        future_better = any(
            abs(p["perKwh"]) > export_cents * 1.3
            for p in analysis["best_sell_windows"][:3]
        )
        if future_better:
            return "⏳ HOLD", f"Export good ({export_cents:.1f}c) but higher prices coming ({best_export:.0f}c). Wait.", 0.7
        return "💰 SELL TO GRID", f"High export ({export_cents:.1f}c). Best window — discharge to grid", 0.85

    # 4. Solar generating — store it
    if solar_kw > 0.5:
        learned_export = analysis.get("learned_export_kw", 0)
        # If learned profile says we normally export more, trust that
        expected_solar = max(solar_kw, learned_export)
        if export_cents > avg_price * 0.8:
            return "☀️ SOLAR → GRID", f"Solar ~{expected_solar:.1f}kW. Export decent ({export_cents:.1f}c) — sell direct", 0.7
        return "☀️ SOLAR → BATTERY", f"Solar ~{expected_solar:.1f}kW. Low export ({export_cents:.1f}c) — store for peak", 0.8

    # 5. Peak/shoulder tariff — self consume (tariff-aware)
    learned_load = analysis.get("learned_load_kw", None)
    if is_peak and import_cents > avg_price * 0.8:
        load_str = f", expected load {learned_load:.1f}kW" if learned_load else ""
        saving = import_cents - cycle_cost
        return "🏠 SELF-CONSUME", f"[PEAK tariff] import {import_cents:.1f}c{load_str}. Save {saving:.1f}c/kWh", 0.9

    if is_shoulder and import_cents > avg_price:
        load_str = f", expected load {learned_load:.1f}kW" if learned_load else ""
        saving = import_cents - cycle_cost
        return "🏠 SELF-CONSUME", f"[SHOULDER tariff] import {import_cents:.1f}c (above avg {avg_price:.0f}c){load_str}. Save {saving:.1f}c/kWh", 0.75

    if 16 <= hour < 21 and import_cents > avg_price:
        load_str = f", expected load {learned_load:.1f}kW" if learned_load else ""
        saving = import_cents - cycle_cost
        return "🏠 SELF-CONSUME", f"Evening peak, import {import_cents:.1f}c (above avg {avg_price:.0f}c){load_str}. Save {saving:.1f}c/kWh", 0.8

    # 6. Above-base load during non-offPeak — mild self-consume
    base_load = analysis.get("base_load_kw", 0.15)
    if learned_load and learned_load > base_load * 2 and import_cents > avg_price * 1.1:
        tariff_str = f" [{tariff_period}]" if tariff_period != "offPeak" else ""
        return "🏠 SELF-CONSUME", f"Above-base load ({learned_load:.1f}kW vs {base_load:.2f}kW){tariff_str}, {import_cents:.1f}c > avg", 0.6

    # 7. Nothing compelling
    return "😴 IDLE", f"No clear opportunity. Import {import_cents:.1f}c, export {export_cents:.1f}c, avg {avg_price:.0f}c [{tariff_period}]", 0.5


def format_time(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%H:%M")
    except Exception:
        return iso_str


def print_dashboard(
    current: list[dict],
    analysis: dict,
    weather: dict,
    aemo: dict,
    action: str,
    reason: str,
    confidence: float,
    solar_kw: float,
    cycle: int,
    day_plan: DayPlan | None = None,
):
    now = datetime.now()
    import_p = next((p for p in current if p.get("channelType") == "general"), {})
    export_p = next((p for p in current if p.get("channelType") == "feedIn"), {})

    import_cents = import_p.get("perKwh", 0)
    export_cents = abs(export_p.get("perKwh", 0))
    spot = import_p.get("spotPerKwh", 0)
    renewables = import_p.get("renewables", 0)
    tariff = import_p.get("tariffInformation", {})
    descriptor = import_p.get("descriptor", "?")
    spike = import_p.get("spikeStatus", "none")

    hour = now.hour
    temp = weather.get("temperature_2m", [0] * 24)
    cloud = weather.get("cloud_cover", [0] * 24)
    current_temp = temp[hour] if hour < len(temp) else 0
    current_cloud = cloud[hour] if hour < len(cloud) else 0

    aemo_demand = aemo.get("TOTALDEMAND", 0)
    aemo_price = aemo.get("PRICE", 0)

    print(f"\n{'='*70}")
    print(f"  GRIDPILOT  │  {now.strftime('%Y-%m-%d %H:%M:%S')}  │  Cycle #{cycle}")
    print(f"{'='*70}")

    print(f"\n  📊 CURRENT PRICES")
    print(f"     Import:  {import_cents:>8.2f} c/kWh   (spot: {spot:.2f}c)")
    print(f"     Export:  {export_cents:>8.2f} c/kWh")
    print(f"     Spread:  {import_cents - export_cents:>8.2f} c/kWh")
    print(f"     Amber:   {descriptor}  │  Spike: {spike}  │  Tariff: {tariff.get('period', '?')}/{tariff.get('season', '?')}")

    print(f"\n  🔮 FORECAST (next 48h)")
    print(f"     Import:  min {analysis['forecast_min']:>6.1f}c  │  avg {analysis['forecast_avg']:>6.1f}c  │  max {analysis['forecast_max']:>6.1f}c")
    print(f"     Export:  avg {analysis['export_avg']:>6.1f}c  │  max {analysis['export_max']:>6.1f}c")
    print(f"     Negative intervals: {analysis['negative_intervals']}  │  Spike risk: {analysis['spike_intervals']}")

    if analysis["cheapest_windows"]:
        print(f"\n     Cheapest buy windows:")
        for p in analysis["cheapest_windows"][:3]:
            print(f"       {format_time(p['startTime'])} — {p['perKwh']:.1f}c")

    if analysis["best_sell_windows"]:
        print(f"     Best sell windows:")
        for p in analysis["best_sell_windows"][:3]:
            print(f"       {format_time(p['startTime'])} — {abs(p['perKwh']):.1f}c")

    print(f"\n  ☀️ WEATHER")
    print(f"     Solar est:  {solar_kw:.2f} kW  │  Cloud: {current_cloud:.0f}%  │  Temp: {current_temp:.1f}°C")
    print(f"     Renewables: {renewables:.1f}%")

    if aemo_demand:
        print(f"\n  🔌 NEM GRID (NSW1)")
        print(f"     Demand: {aemo_demand:.0f} MW  │  Dispatch price: ${aemo_price:.2f}/MWh")

    if analysis.get("learned_load_kw") is not None:
        print(f"\n  📈 LEARNED PROFILE")
        print(f"     Expected load:   {analysis['learned_load_kw']:.2f} kW  │  Expected export: {analysis.get('learned_export_kw', 0):.2f} kW")
        print(f"     Base load:       {analysis.get('base_load_kw', 0):.2f} kW  │  Solar peak: {analysis.get('solar_peak_kw', 0):.1f} kW")

    if day_plan and day_plan.schedule:
        print(f"\n{format_plan(day_plan)}")

    print(f"\n  {'─'*66}")
    print(f"  🤖 GRIDPILOT SAYS:  {action}")
    print(f"     {reason}")
    print(f"     Confidence: {'█' * int(confidence * 10)}{'░' * (10 - int(confidence * 10))} {confidence:.0%}")
    print(f"{'='*70}\n")


def _profile_stale(profile) -> bool:
    """Re-learn if profile is older than 24 hours."""
    try:
        updated = datetime.fromisoformat(profile.last_updated)
        return (datetime.now() - updated).total_seconds() > 86400
    except Exception:
        return True


async def run():
    config = Config()
    if not config.amber_api_token:
        try:
            with open("/home/zhanpeng/repo/own/amber") as f:
                lines = f.read().strip().split("\n")
                config.amber_api_token = lines[1] if len(lines) > 1 else lines[0]
        except FileNotFoundError:
            logger.error("No Amber API token. Set AMBER_API_TOKEN or create ~/repo/own/amber")
            return

    if not config.amber_site_id:
        config.amber_site_id = "01K586V49X2WQ2EBY00YANFP8N"

    # Learn from history on startup
    from src.data.learner import UsageLearner, DecisionReviewer
    learner = UsageLearner(config.amber_api_token, config.amber_site_id)
    profile = learner.load_cached()
    if profile is None or _profile_stale(profile):
        logger.info("Learning usage profile from history...")
        try:
            profile = await learner.learn(days_back=30)
        except Exception as e:
            logger.warning("Learning failed: %s — using defaults", e)
            profile = None

    if profile:
        logger.info("Profile: base load %.2fkW, solar peak %.1fkW, "
                     "peak import %d:00, peak export %d:00, %d days analysed",
                     profile.base_load_kw, profile.solar_peak_kw,
                     profile.peak_import_hour, profile.peak_export_hour,
                     profile.days_analysed)

    # Review past decisions
    reviewer = DecisionReviewer()
    review = reviewer.review(days_back=7)
    if review["total"] > 0:
        logger.info("Decision review: %d decisions, %.1f%% accuracy, %d bad calls",
                     review["total"], review["accuracy_pct"], len(review["bad_calls"]))

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    logger.info("GridPilot monitor starting — %ds cycle", config.decision_interval_seconds)

    cycle = 0
    day_plan: DayPlan | None = None
    last_plan_hour = -1

    # Build profile dict for planner
    profile_dict = None
    if profile:
        profile_dict = {
            "hours": [vars(h) for h in profile.hours],
        }

    planner_config = {
        "battery_capacity_kwh": config.battery_capacity_kwh,
        "round_trip_efficiency": config.battery_round_trip_efficiency,
        "cycle_cost_cents": config.battery_cycle_cost_cents,
        "max_charge_kw": config.battery_max_charge_kw,
        "max_discharge_kw": config.battery_max_discharge_kw,
        "min_soc_pct": config.battery_min_soc_pct,
    }

    async with httpx.AsyncClient(timeout=15) as http:
        while not shutdown.is_set():
            cycle += 1
            try:
                current, all_prices = await fetch_amber(http, config.amber_api_token, config.amber_site_id)
                weather = await fetch_weather(http, config.latitude, config.longitude)
                aemo = await fetch_aemo(http, config.nem_region)

                analysis = analyse_forecast(all_prices)
                hour = datetime.now().hour
                minute = datetime.now().minute
                solar_kw = estimate_solar(weather, hour)

                # Use learned profile for load prediction
                if profile:
                    is_weekday = datetime.now().weekday() < 5
                    predicted_load = profile.predicted_import_kw(hour, is_weekday)
                    predicted_export = profile.predicted_export_kw(hour, is_weekday)
                    analysis["learned_load_kw"] = round(predicted_load, 2)
                    analysis["learned_export_kw"] = round(predicted_export, 2)
                    analysis["base_load_kw"] = profile.base_load_kw
                    analysis["solar_peak_kw"] = profile.solar_peak_kw

                import_cents = next((p["perKwh"] for p in current if p.get("channelType") == "general"), 30)
                export_cents = abs(next((p["perKwh"] for p in current if p.get("channelType") == "feedIn"), 5))
                spike = next((p.get("spikeStatus", "none") for p in current if p.get("channelType") == "general"), "none")

                # ── Rebuild day plan every 30 min or on first run ──
                if day_plan is None or hour != last_plan_hour or (minute < 6 and last_plan_hour == hour):
                    general_prices = [p for p in all_prices if p.get("channelType") == "general"]
                    feedin_prices = [p for p in all_prices if p.get("channelType") == "feedIn"]
                    day_plan = build_day_plan(general_prices, feedin_prices, weather, profile_dict, planner_config)
                    last_plan_hour = hour
                    logger.info("Day plan rebuilt: %d arbitrage pairs, expected value %.0fc",
                                day_plan.summary["arbitrage_pairs"],
                                day_plan.summary["total_expected_cents"])

                # ── Check for real-time overrides (spikes, negative prices) ──
                override, override_action, override_reason = should_override(
                    day_plan, import_cents, export_cents, spike)

                if override:
                    action = {"charge_grid": "⚡ CHARGE FROM GRID", "sell_grid": "💰 SELL TO GRID",
                              "discharge_house": "🛡️ SPIKE SHIELD"}.get(override_action, override_action)
                    reason = f"⚠️ OVERRIDE: {override_reason}"
                    confidence = 0.95
                else:
                    # Follow the day plan
                    planned = day_plan.action_for_time(hour, minute)
                    if planned:
                        action = {"charge_grid": "⚡ CHARGE FROM GRID", "sell_grid": "💰 SELL TO GRID",
                                  "self_consume": "🏠 SELF-CONSUME", "charge_solar": "☀️ SOLAR → BATTERY"
                                  }.get(planned.action, planned.action)
                        reason = f"📋 PLAN: {planned.reason}"
                        confidence = 0.8
                    else:
                        # No plan for this slot — fall back to per-interval logic
                        tariff_period = next(
                            (p.get("tariffInformation", {}).get("period", "offPeak")
                             for p in current if p.get("channelType") == "general"), "offPeak")
                        action, reason, confidence = gridpilot_recommendation(
                            import_cents, export_cents, analysis, solar_kw, hour, config, tariff_period,
                        )

                print_dashboard(current, analysis, weather, aemo, action, reason, confidence, solar_kw, cycle, day_plan)

                with open("data/decisions.log", "a") as f:
                    f.write(f"{datetime.now().isoformat()}|{action}|{import_cents:.2f}|{export_cents:.2f}|"
                            f"{analysis['forecast_avg']:.1f}|{analysis['forecast_max']:.1f}|"
                            f"{solar_kw:.2f}|{confidence:.2f}|{reason}\n")

            except Exception as e:
                logger.error("Cycle %d failed: %s", cycle, e, exc_info=True)

            # Re-learn profile daily at 2am
            if cycle > 1 and datetime.now().hour == 2 and datetime.now().minute < 6:
                try:
                    profile = await learner.learn(days_back=30)
                    review = reviewer.review(days_back=7)
                    logger.info("Daily re-learn complete. Accuracy: %.1f%%", review["accuracy_pct"])
                    profile_dict = {"hours": [vars(h) for h in profile.hours]} if profile else None
                except Exception as e:
                    logger.warning("Daily re-learn failed: %s", e)

            try:
                await asyncio.wait_for(shutdown.wait(), timeout=config.decision_interval_seconds)
                break
            except asyncio.TimeoutError:
                pass

    logger.info("GridPilot monitor stopped after %d cycles", cycle)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
