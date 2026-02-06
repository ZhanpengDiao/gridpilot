"""Health monitoring and alerting."""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    last_successful_cycle: datetime | None = None
    consecutive_failures: int = 0
    total_cycles: int = 0
    total_failures: int = 0
    api_status: dict = field(default_factory=lambda: {
        "amber": True, "weather": True, "aemo": True,
    })
    uptime_start: float = field(default_factory=time.monotonic)

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.uptime_start

    @property
    def is_degraded(self) -> bool:
        return not all(self.api_status.values())

    @property
    def is_critical(self) -> bool:
        return self.consecutive_failures >= 3 or not self.api_status["amber"]


class HealthMonitor:
    def __init__(self, max_failures_before_alert: int = 3):
        self.status = HealthStatus()
        self._max_failures = max_failures_before_alert

    def record_success(self):
        self.status.last_successful_cycle = datetime.now()
        self.status.consecutive_failures = 0
        self.status.total_cycles += 1

    def record_failure(self, error: str):
        self.status.consecutive_failures += 1
        self.status.total_failures += 1
        self.status.total_cycles += 1
        if self.status.consecutive_failures >= self._max_failures:
            self._alert(f"GridPilot: {self.status.consecutive_failures} consecutive failures. Last: {error}")

    def record_api_status(self, api_name: str, healthy: bool):
        self.status.api_status[api_name] = healthy
        if not healthy:
            logger.warning("API degraded: %s", api_name)

    def _alert(self, message: str):
        """Send alert. TODO: integrate with pushover/ntfy/email."""
        logger.critical("ALERT: %s", message)

    def summary(self) -> str:
        s = self.status
        apis = ", ".join(f"{k}:{'OK' if v else 'DOWN'}" for k, v in s.api_status.items())
        return (
            f"uptime={s.uptime_seconds/3600:.1f}h "
            f"cycles={s.total_cycles} failures={s.total_failures} "
            f"consecutive_fail={s.consecutive_failures} apis=[{apis}]"
        )
