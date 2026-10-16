# funding-capture

A toolkit for finding out whether delta-neutral funding-rate capture is profitable, then
running it: a cross-venue **scanner**, a historical **backtester** with walk-forward
parameter sweeps, and a **paper/live trading engine** with leg-risk-aware execution and
continuous risk monitoring.

Start with [docs/STRATEGY.md](docs/STRATEGY.md). It covers the search space (which
hedged constructions exist, the cost and capital arithmetic, the risks) and the plan for
validating the idea before risking money. **In the US?** Follow
[docs/RUNBOOK_US.md](docs/RUNBOOK_US.md): it walks through every step on a Windows PC
in Git Bash (with notes for Mac/Linux) and covers which venues US persons can
actually trade.

> **Status:** The data, backtest and paper paths are covered by offline tests. The live
> order path is written against ccxt's unified API but has **never been run against a
> real exchange**. Paper trade first, then go live with small size on one venue pair.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
# Windows (Git Bash): py -3.12 -m venv .venv && source .venv/Scripts/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml   # edit venues, fees, sizing
```

## Workflow

```bash
# 0. Connectivity, market data and (once keys are set) account access per venue
fcap -c config.yaml check

# 1. What's on offer right now (public data, no keys)
fcap -c config.yaml scan --top 30

# 2. Download a year of settled funding (top 150 perps by volume per venue)
fcap -c config.yaml fetch --days 365

# 3. Backtest the configured strategy, write the trade log
fcap -c config.yaml backtest --trades trades.csv

# 4. Walk-forward parameter sweep (pick on the first half, judge on the second)
fcap -c config.yaml sweep

# 5. Paper trade on live data with simulated fills and funding
fcap -c config.yaml paper

# 6. Positions and P&L of the running (or last) paper engine
fcap -c config.yaml status

# Venues with no funding-history API (Coinbase US perps): log it forward, or import a CSV
fcap -c config.yaml record
fcap -c config.yaml import-funding --venue coinbase --symbol BIP-20DEC30-CDE --file f.csv

# Symbols and contract sizes as the bot sees them
fcap -c config.yaml markets --venue coinbase --kind perp

# 7. Real orders (needs `mode: live` in config, API keys in env or .env, and the flag)
fcap -c config.yaml live --i-understand-this-trades-real-money
```

Paper and live keep separate state in `state/paper/` and `state/live/` (older versions
used `state/engine.json`; the engine refuses to start until you move that file). Kill switches
while the engine runs: `touch state/<mode>/STOP` blocks new entries;
`touch state/<mode>/FLATTEN` closes every pair and stops opening new ones;
`touch state/<mode>/QUIT` ends the program between steps (open pairs stay open and are
picked up on restart). Use QUIT rather than Ctrl+C in Git Bash on Windows.

Legs on venues listed in `costs.maker_venues` are worked with post-only orders (rest at
the touch, hedge each fill immediately on the other leg). That matters on venues where
taker fees are high, such as US spot exchanges.

## What it trades

| Construction | Legs | Earns |
|---|---|---|
| `spot_perp` | long spot, short perp | positive funding |
| `perp_perp` | long perp where funding is low, short perp where it is high | the spread |
| `reverse_spot_perp` | short spot (borrowed), long perp | negative funding minus borrow (scan/backtest only for now) |

Every candidate is scored as *forecast carry over the hold horizon minus round-trip fees
and slippage, per dollar of capital tied up*. Funding is normalised per hour, so 1h, 4h
and 8h venues compare correctly.

## Layout

```
src/funding_capture/
  models.py          instruments, candidates, snapshots, positions (canonical units)
  symbols.py         1000PEPE / kPEPE / UBTC -> canonical asset + multiplier
  rates.py           interval handling, carry arithmetic
  costs.py           fee schedules, round-trip cost, capital per notional
  forecast.py        EWMA + mean-reverting horizon forecast (batch and streaming)
  opportunities.py   candidate enumeration, scoring, entry/exit rules
  data.py            on-disk funding history + ccxt downloader
  backtest.py        hourly event-driven backtest, walk-forward sweep
  venues/ccxt_venue  ccxt adapter (data + orders), unit conversion at the boundary
  venues/coinbase    Coinbase spot + US perpetual-style futures via raw Advanced Trade calls
  venues/paper.py    simulated fills/funding on top of live data
  execution.py       two-leg IOC executor: thin leg first, hedge, unwind on failure
  risk.py            delta drift / ADL, liquidation distance, divergence, stop loss
  engine.py          the loop: data -> funding P&L -> risk -> exits -> entries -> state
  scanner.py, cli.py, config.py, state.py
```

## Tests

```bash
pytest          # offline; synthetic data only checks mechanics, not profitability
ruff check src tests
```

## Safety notes

* API keys come from environment variables or a git-ignored `.env` file
  (`<VENUE>_API_KEY`, `<VENUE>_SECRET`, `<VENUE>_PASSWORD`, `<VENUE>_WALLET_ADDRESS`,
  `<VENUE>_PRIVATE_KEY`; any of them can be `<NAME>_FILE=/path` instead, for PEM keys).
  Create keys with trading permission only, no withdrawals, and IP-allowlist them.
* Give the bot dedicated sub-accounts. It assumes it owns every position on the
  instruments it trades, and closes positions it believes are unhedged.
* Check that you are legally allowed to use each venue from where you live.
