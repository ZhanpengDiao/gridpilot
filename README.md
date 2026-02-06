# Solar Battery Amber VPP

Residential solar battery management system integrated with [Amber Electric's](https://www.amber.com.au/) Virtual Power Plant (VPP).

## Overview

This project automates residential solar battery charge/discharge decisions based on Amber Electric's real-time wholesale electricity pricing and VPP signals to maximise savings and grid revenue.

## Features

- Real-time Amber Electric price monitoring
- Automated battery charge/discharge scheduling based on price signals
- Amber VPP event participation (feed-in on demand)
- Battery state-of-charge management
- Dashboard for monitoring and manual overrides

## Getting Started

### Prerequisites

- Amber Electric account with API access
- Compatible solar battery inverter (e.g. Tesla Powerwall, SolarEdge, Enphase)
- Python 3.11+

### Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Amber API token and battery config
```

## License

MIT
