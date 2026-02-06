"""GridPilot main loop — collect data, decide, act, repeat."""
import asyncio
import logging
import signal
import sys

from src.core.config import Config
from src.core.health import HealthMonitor
from src.data.collector import DataCollector
from src.data.decision_log import DecisionLog
from src.strategy.engine import StrategyEngine
from src.strategy.fallback import FallbackStrategy

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("gridpilot")


async def run():
    config = Config()
    collector = DataCollector(config)
    strategy = StrategyEngine(config)
    fallback = FallbackStrategy(config)
    health = HealthMonitor()
    log = DecisionLog()

    shutdown = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    logger.info("GridPilot starting — cycle every %ds", config.decision_interval_seconds)
    logger.info("Battery: %.1f kWh | Min SOC: %.0f%% | Efficiency: %.0f%%",
                config.battery_capacity_kwh, config.battery_min_soc_pct,
                config.battery_round_trip_efficiency * 100)

    try:
        while not shutdown.is_set():
            try:
                snap = await collector.collect()

                # Track API health
                health.record_api_status("amber", snap.current_import_price is not None)
                health.record_api_status("weather", len(snap.solar_forecast) > 0)

                # Use fallback if Amber data is missing
                if snap.current_import_price is None:
                    decision = fallback.decide(snap.battery)
                else:
                    decision = strategy.decide(snap)

                health.record_success()
                log.record(decision)

                logger.info(
                    "%-18s | %5.1f kW | SOC: %4.0f%% | Import: %6.1fc | Export: %6.1fc | %s",
                    decision.action.value,
                    decision.power_kw,
                    snap.battery.soc_pct,
                    decision.factors.get("import_cents", 0),
                    decision.factors.get("export_cents", 0),
                    decision.reason,
                )

                # TODO: Send command to battery inverter API

            except Exception as e:
                health.record_failure(str(e))
                logger.error("Cycle failed: %s", e, exc_info=True)

                # Fallback on total failure
                try:
                    decision = fallback.decide(collector._default_battery())
                    log.record(decision)
                    logger.info("FALLBACK: %s", decision.reason)
                except Exception:
                    pass

            # Log health every 12 cycles (~1 hour at 5min intervals)
            if health.status.total_cycles % 12 == 0:
                logger.info("Health: %s", health.summary())

            try:
                await asyncio.wait_for(shutdown.wait(), timeout=config.decision_interval_seconds)
                break
            except asyncio.TimeoutError:
                pass
    finally:
        await collector.close()
        logger.info("GridPilot stopped. %s", health.summary())


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
