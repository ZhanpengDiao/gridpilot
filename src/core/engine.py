"""GridPilot main loop — collect data, decide, act, repeat."""
import asyncio
import logging
from datetime import datetime

from src.core.config import Config
from src.data.collector import DataCollector
from src.strategy.engine import StrategyEngine

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("gridpilot")


async def run():
    config = Config()
    collector = DataCollector(config)
    strategy = StrategyEngine(config)

    logger.info("GridPilot starting — decision cycle every %ds", config.decision_interval_seconds)
    logger.info("Battery: %.1f kWh, min SOC: %.0f%%, efficiency: %.0f%%",
                config.battery_capacity_kwh, config.battery_min_soc_pct,
                config.battery_round_trip_efficiency * 100)

    try:
        while True:
            try:
                snap = await collector.collect()
                decision = strategy.decide(snap)

                logger.info(
                    "ACTION: %-18s | %5.1f kW | SOC: %4.0f%% | Import: %6.1fc | Export: %6.1fc | %s",
                    decision.action.value,
                    decision.power_kw,
                    snap.battery.soc_pct,
                    decision.factors.get("import_cents", 0),
                    decision.factors.get("export_cents", 0),
                    decision.reason,
                )

                # TODO: Send command to battery inverter API
                # For now, log-only mode (dry run)

            except Exception as e:
                logger.error("Decision cycle failed: %s", e, exc_info=True)

            await asyncio.sleep(config.decision_interval_seconds)
    finally:
        await collector.close()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
