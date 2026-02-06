# GridPilot

Autopilot for your residential solar battery — integrated with [Amber Electric's](https://www.amber.com.au/) Virtual Power Plant (VPP).

## Overview

GridPilot is an intelligent battery management system that maximises savings and profit for residential solar+battery systems on Amber Electric. It goes beyond Amber's built-in SmartShift by combining real-time pricing, weather forecasts, AEMO grid data, battery characteristics, and household usage patterns into an optimised charge/discharge strategy.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    GridPilot                         │
│                                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────┐  │
│  │  Data Layer  │  │  Strategy   │  │  Control   │  │
│  │             │  │   Engine    │  │   Layer    │  │
│  │ • Amber API │  │             │  │            │  │
│  │ • Weather   │──▶ • Forecast  │──▶│ • Battery  │  │
│  │ • AEMO      │  │ • Optimiser │  │   Commands │  │
│  │ • Battery   │  │ • Arbitrage │  │ • Logging  │  │
│  │ • Usage     │  │ • Spike     │  │ • Alerts   │  │
│  └─────────────┘  └─────────────┘  └────────────┘  │
└─────────────────────────────────────────────────────┘
```

## Data Sources

| Source | Data | Purpose |
|--------|------|---------|
| Amber API | Real-time & forecast prices, usage, battery SOC, solar output, VPP events | Primary pricing & device state |
| Bureau of Meteorology / OpenMeteo | Solar irradiance, cloud cover, temperature | Solar generation forecast |
| AEMO | Grid demand, interconnector flows, generation mix | Price movement prediction |
| Device | Battery capacity, charge/discharge rate, round-trip efficiency, cycle limits | Physical constraints |
| Historical | Past usage patterns, price patterns | Load & price prediction |

## Decision Factors

The strategy engine weighs all of these for every 5-minute decision cycle:

### Price Signals
- Current import price (c/kWh)
- Current export/feed-in price (c/kWh)
- Amber 30-min interval forecast (next 12-56 hours)
- Price spike indicators (none / potential / actual)
- Controlled load pricing windows

### Weather & Solar
- Current solar irradiance and cloud cover
- Hourly solar generation forecast for remainder of day + next day
- Temperature (affects battery efficiency and household HVAC load)

### AEMO Grid State
- NEM region demand (SA, VIC, NSW, QLD, TAS)
- Scheduled vs actual generation
- Interconnector flows (import/export between states)
- Renewable generation percentage
- Demand response signals

### Battery Constraints
- Current state of charge (SOC %)
- Usable capacity (kWh) accounting for degradation
- Max charge rate (kW) — can't charge faster than inverter allows
- Max discharge rate (kW)
- Round-trip efficiency (~87-93% depending on chemistry)
- Cycle count / degradation cost per cycle
- Minimum reserve SOC (blackout protection)
- Thermal limits (temperature-dependent derating)

### Household Load
- Learned daily consumption profile (weekday/weekend/seasonal)
- Predicted load for next 24 hours
- Known scheduled loads (EV charging, pool pump, HVAC)

## Strategy Modes

| Mode | When | Action |
|------|------|--------|
| **Spike Shield** | Price spike detected/imminent | Discharge battery for house, avoid grid |
| **Grid Charge** | Price negative or below threshold | Charge from grid (get paid to store) |
| **Solar Charge** | Solar excess, battery not full | Store solar for later use/sale |
| **Peak Sell** | Export price high, future prices lower | Discharge to grid for revenue |
| **Self Consume** | Medium price, battery has charge | Use battery instead of grid |
| **VPP Dispatch** | Amber VPP event active | Max discharge to grid for VPP bonus |
| **Idle** | Battery full + solar covering load | Do nothing, direct solar to grid |

## Getting Started

### Prerequisites
- Python 3.11+
- Amber Electric account with API token
- Compatible solar battery inverter

### Setup
```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your configuration
python -m src.core.engine
```

## License

MIT
