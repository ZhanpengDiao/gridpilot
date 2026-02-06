"""AEMO NEM data for grid state awareness."""
import logging
from datetime import datetime

import httpx

from src.models.types import GridState

logger = logging.getLogger(__name__)


class AEMOClient:
    """Pulls public NEM dispatch/demand data."""

    # AEMO public data endpoints
    DISPATCH_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY"

    def __init__(self, nem_region: str = "NSW1"):
        self._region = nem_region
        self._http = httpx.AsyncClient(timeout=15)

    async def get_grid_state(self) -> GridState:
        """Get current NEM grid state for the configured region."""
        try:
            resp = await self._http.get(self.DISPATCH_URL)
            resp.raise_for_status()
            data = resp.json()

            region_data = self._find_region(data)
            return GridState(
                timestamp=datetime.now(),
                nem_region=self._region,
                demand_mw=region_data.get("TOTALDEMAND", 0),
                price_aud_mwh=region_data.get("PRICE", 0),
                renewables_pct=self._calc_renewables_pct(region_data),
                interconnector_flow_mw=region_data.get("NETINTERCHANGE", 0),
            )
        except Exception as e:
            logger.warning("AEMO data unavailable: %s", e)
            return GridState(
                timestamp=datetime.now(),
                nem_region=self._region,
                demand_mw=0,
                price_aud_mwh=0,
                renewables_pct=0,
                interconnector_flow_mw=0,
            )

    def _find_region(self, data: list | dict) -> dict:
        if isinstance(data, list):
            for entry in data:
                if entry.get("REGIONID") == self._region:
                    return entry
        return {}

    def _calc_renewables_pct(self, data: dict) -> float:
        total = data.get("TOTALDEMAND", 1)
        solar = data.get("SOLAR", 0)
        wind = data.get("WIND", 0)
        return round((solar + wind) / max(total, 1) * 100, 1) if total else 0

    async def close(self):
        await self._http.aclose()
