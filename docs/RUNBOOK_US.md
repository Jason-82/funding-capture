# Runbook: running funding-capture step by step (US)

Every command here runs **on your own computer**. The cloud sandbox this was built in
can't reach exchange APIs.

Throughout, `$` lines are commands to paste into a terminal. Run them from the project
folder with the virtual environment active (step 1).

<!-- VENUES -->

---

## 1. Install (once)

**macOS**

```bash
$ xcode-select --install          # only if `git --version` says git is missing
$ brew install python@3.12        # or install Python 3.11+ from python.org
$ git clone https://github.com/Jason-82/funding-capture.git
$ cd funding-capture
$ git checkout claude/busy-maxwell-hiuy46     # until this branch is merged to main
$ python3.12 -m venv .venv
$ source .venv/bin/activate       # do this in every new terminal before using fcap
$ pip install -e ".[dev]"
$ pytest -q                       # should end with "... passed"
```

**Windows:** easiest is WSL (PowerShell as admin: `wsl --install`, reboot, open
"Ubuntu"), then run the same commands, using `sudo apt install python3-venv git`
instead of brew. Native Windows also works: install Python 3.12 from python.org, then
`py -3.12 -m venv .venv` and `.venv\Scripts\Activate.ps1`.

Updating later: `git pull` then `pip install -e ".[dev]"`.

## 2. Create your config

```bash
$ cp config.us.yaml config.yaml
```

Open `config.yaml` in an editor. The settings to look at first:

| Key | What it does | Start with |
|---|---|---|
| `venues` | which exchanges to scan/trade, spot-only or perp | as shipped |
| `fees` | your **actual** fee tier per venue; this decides which trades pass | your tier |
| `costs.maker_venues` | legs on these venues are worked with post-only (maker) orders | `[kraken, coinbase]` |
| `strategy.entry_apr` | minimum expected APR on capital after costs to open a pair | `0.20` |
| `portfolio.capital_usd` / `max_position_usd` | total capital and size per pair | small |
| `portfolio.min_volume_usd` | skip coins with less 24h volume than this on either leg | `1000000` |

`config.yaml` is git-ignored, so your edits stay local.

## 3. Check connectivity (no keys needed)

```bash
$ fcap -c config.yaml check
```

You should see `ok markets`, `ok funding` and `ok quotes` for each venue, and
`skip account checks` (no keys yet). A `FAIL` line names the venue and the error.

## 4. Look at live opportunities

```bash
$ fcap -c config.yaml scan --top 40
```

Columns:

* **gross now**: current funding carry APR on notional (before costs)
* **fcst**: what the forecaster expects over the hold horizon (funding fades)
* **net/cap**: expected APR **on your capital** after round-trip fees and slippage; the
  number that matters
* **b/e h**: hours the pair must be held to pay back its fees
* **vol $M**: the smaller 24h volume of the two legs
* **div**: price disagreement between the legs (should be ~0; large means they aren't
  the same coin)
* `*`: passes your entry rules

`--all` also shows rows that fail the liquidity/spread filters.

Run it a few times over a couple of days. If nothing ever shows `*`, either the market
is quiet or your fees are too high for your thresholds. The backtest will tell you which.

## 5. Download history and backtest

```bash
$ fcap -c config.yaml fetch --days 180            # 10-40 min the first time; resumable
$ fcap -c config.yaml backtest --trades trades.csv
$ fcap -c config.yaml sweep                       # ~5-15 min
```

* `fetch` saves funding history for each perp venue's most-traded markets (top 150 by
  volume; change with `--max-markets`, `--min-volume`). It also saves which spot markets
  your spot venues list, so cross-venue pairs can be simulated. Re-running only
  downloads what's new.
* `backtest` prints P&L, APR on capital, the funding vs. cost split, win rate, average
  hold, how much capital was working, max drawdown, and a per-construction breakdown.
  `trades.csv` has every simulated trade; open it in Excel/Numbers.
* `sweep` tries a grid of entry/exit thresholds, horizons and forecast settings. It picks
  on the first half of the data and reports the second half (`test_apr_pct`). Choose
  settings whose **test** results are good and stable across neighbouring rows, not the
  single best training row. Put them in `config.yaml` under `strategy:`.

**Go / no-go.** Continue only if `APR on capital` (test half) comfortably beats what
you'd earn holding USDC risk-free, plus a premium for exchange risk (say 10%+ above
it). Check that it survives the stress tests:

```bash
# in config.yaml: raise costs.slippage_bps to 10, portfolio.min_volume_usd to 5000000
$ fcap -c config.yaml backtest
```

If the result collapses, the edge was in coins too thin to trade.

What the backtest leaves out (live will be somewhat worse): price gaps between legs at
entry/exit, liquidations, coins delisted before you downloaded data, and whether your
maker orders actually get filled.

<!-- ACCOUNTS -->

## 7. Paper trade (2–4 weeks)

Paper mode uses live prices and funding but simulated fills, with fees from your config.
It needs no API keys and places no orders.

```bash
$ mkdir -p logs
$ fcap -c config.yaml --log-file logs/paper.log paper
```

Leave it running. Use a spare machine or a cheap always-on VPS, or keep the laptop awake
(`caffeinate -i fcap ...` on macOS). To survive closing the terminal:

```bash
$ tmux new -s paper                       # then run the paper command inside it
# detach: Ctrl-b then d      re-attach later: tmux attach -t paper
```

In another terminal (venv active):

```bash
$ fcap -c config.yaml status              # open pairs, funding earned, fees, closed P&L
$ tail -f logs/paper.log                  # live log; Ctrl-c to stop watching
```

State is saved in `state/paper/`, so stopping and restarting picks up where it left off.
To start over, stop it and delete `state/paper/`.

**Kill switches** (paper or live):

```bash
$ touch state/paper/STOP        # no new pairs; existing ones are still managed
$ rm state/paper/STOP           # resume
$ touch state/paper/FLATTEN     # close everything now and stop opening
```

(For live, the folder is `state/live/`.)

**What to look for after 2–4 weeks:**

* realised funding per pair vs. what `scan`/`backtest` expected
* fees and slippage per round trip vs. the config's assumptions
* how often maker orders timed out (`grep "timed out" logs/paper.log`)
* any `risk CLOSE` / `CRITICAL` lines, and why

Paper fills for maker orders are optimistic (no queue), so real maker fills will be
fewer and slower.

<!-- LIVE -->

## Daily operations checklist

* `fcap -c config.yaml status --live`: anything stuck, partially closed, or old?
* `grep -E "CRITICAL|risk CLOSE|FAIL" logs/live.log`: investigate every hit.
* Perp venue margin: is account equity comfortably above what the open shorts need?
  Top up before you get close; the bot closes pairs at 20% from liquidation, which
  costs fees.
* Venue status pages and announcements: delistings, maintenance, deposit/withdrawal
  suspensions.
* Once a week, compare the bot's view of positions with each exchange's UI.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `FAIL markets: NetworkError` | no internet, VPN/firewall, or the venue is down; retry |
| `FAIL balance: AuthenticationError` | wrong key/secret, key lacks a permission, or IP allowlist excludes you |
| `scan` shows nothing with `*` | thresholds too strict for current markets or fees too high; try `--all`, check `fees` |
| `ENTRIES HALTED: stale market data` | one venue stopped answering; it clears itself when data returns |
| `ENTRIES HALTED: unhedged residual ...` | an execution left a one-sided remainder; fix it by hand on the exchange, then restart the bot |
| `maker order ... timed out` often | spot leg not getting filled; raise `execution.maker_timeout_s` or accept taker fees for that venue |
| `fetch` slow | normal on first run (thousands of API pages); it resumes if interrupted |

## Command reference

```
fcap -c config.yaml check                     connectivity, market data, keys
fcap -c config.yaml scan [--top N] [--all]    live opportunities
fcap -c config.yaml fetch [--days N] [--venues a,b] [--assets BTC,ETH]
                         [--min-volume USD] [--max-markets N]
fcap -c config.yaml backtest [--start YYYY-MM-DD] [--end ...] [--trades file.csv]
fcap -c config.yaml sweep [--split 0.5] [--top N]
fcap -c config.yaml paper [--steps N]
fcap -c config.yaml live --i-understand-this-trades-real-money
fcap -c config.yaml status [--live] [--last N]
global: -v (debug logs)  --log-file FILE  --env-file FILE (default .env)
```
