# Runbook: running funding-capture step by step (US)

Everything here runs **on your own computer** (or a small always-on server). The cloud
sandbox this was built in can't reach exchange APIs, so none of these commands have been
run against a live exchange yet. Steps 3 and 9 exist to catch that early, with no money
or with one contract.

`$` lines are commands to paste into a terminal, from the project folder, with the
virtual environment active (step 1). Venue facts below were researched on 2026-10-15 and
change often: fees especially. The bot prints your real fees in step 8.

---

## 0. What you can trade from the US

| Venue | What the bot uses it for | Status for a US person |
|---|---|---|
| **Coinbase** (Advanced) | spot long legs **and** the short perp leg: CFTC-regulated "perpetual-style" futures on ~20 coins (BTC, ETH, SOL, XRP, DOGE, ADA, LINK, AVAX, LTC, DOT, XLM, HBAR, SUI, BCH, NEAR, AAVE, ONDO, ENA, 1000SHIB, 1000PEPE, ZEC…), funding every hour | ✅ needs futures onboarding in the Coinbase app |
| **Kraken Pro** | spot long legs (lots of alts) | ✅ spot. Not available in NY or ME; no crypto withdrawals from IN |
| Kraken US perps (Bitnomial) | – | exist (9–16 majors, 8h funding) but no retail API, so the bot can't trade them |
| Kalshi | – | has had BTC/ETH/SOL/XRP/HYPE perps since June 2026 (8h funding), but no ccxt support and API access is invite-by-invite; possible future adapter |
| Hyperliquid, Lighter | – | ❌ their terms of use exclude US persons (Hyperliquid §1.6, updated 2026-06-15; Lighter's terms cover its API too) |

So the US version of the trade is: **buy the coin spot on Kraken or Coinbase, short the
same coin's Coinbase perp, and collect the hourly funding while it is positive.**

**Read this before you spend time on it. The economics are tougher than offshore:**

* **Spot fees are high.** Coinbase Intro tier: 0.50% maker / 0.90% taker. Kraken Tier 1:
  reportedly 0.40% / 0.80%. The bot rests post-only (maker) orders on the spot leg, so a
  round trip still costs ~0.8–1.0% plus perp fees. That's why the US config plans to hold
  a position about a month and only enters when expected funding clearly beats that.
* **Capital efficiency is poor.** The spot coins are fully paid for, and Coinbase holds
  30–95% of notional as overnight margin on short alt perps. A $1,000 pair ties up about
  $2,000, so a 30% funding APR is roughly 15% on your capital *before* fees.
* **Fewer markets.** About 20 perps, some with $1–2M/day volume.

Kraken's fees drop with "assets on platform" (e.g. ~$20k held moves you to ~0.22% maker),
and Coinbase tiers drop with volume or USDC balance. Better tiers change the answer a lot.
Run steps 4–6 before funding anything.

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
"Ubuntu"), then the same commands, with `sudo apt install python3-venv git` instead of
brew. Native Windows also works: Python 3.12 from python.org, `py -3.12 -m venv .venv`,
then `.venv\Scripts\Activate.ps1`.

Updating later: `git pull`, then `pip install -e ".[dev]"`.

## 2. Create your config

```bash
$ cp config.us.yaml config.yaml
```

`config.yaml` is git-ignored, so your edits stay local. Every setting is commented. The
ones that matter first:

| Key | What it does |
|---|---|
| `fees` | your **actual** fee tier per venue (step 8 prints it); decides which trades pass |
| `costs.maker_venues` | spot legs on these venues are worked with post-only orders |
| `costs.perp_leverage` | 1.0 = budget margin equal to the perp notional (safe for alts overnight) |
| `strategy.entry_apr` | minimum expected APR **on capital**, after all fees, to open a pair |
| `strategy.horizon_hours` | how long a position is expected to be held when judging entry |
| `portfolio.capital_usd`, `max_position_usd`, `max_positions` | sizing |
| `portfolio.min_volume_usd` | skip coins with less 24h volume than this on either leg |

## 3. Check connectivity (no keys needed)

```bash
$ fcap -c config.yaml check
```

Expected, per venue: `ok markets: N perps, M spot markets`, `ok funding: …/1h` for
Coinbase, `ok quotes: …`, then `skip account checks (no API credentials…)`.

For **coinbase**, `markets` should report around 20 perps. Zero perps means Coinbase
changed something in its product list; tell me and I'll adapt `venues/coinbase.py`.
The funding lines should show small hourly rates such as `+0.000020/1h` (0.002% per hour
≈ 17.5% APR).

## 4. Look at live opportunities

```bash
$ fcap -c config.yaml scan --top 30
$ fcap -c config.yaml scan --top 30 --all     # include pairs failing volume/spread filters
```

Columns:

* **long leg / short leg**: e.g. `kraken:spot` / `coinbase:perp`
* **gross now**: current funding APR on notional, before costs
* **fcst**: forecast funding APR over the hold horizon (funding fades)
* **net/cap**: forecast APR **on capital** after round-trip fees; the number that matters
* **b/e h**: hours the position needs to pay back its fees
* **vol $M**: the smaller 24h volume of the two legs
* **div**: price disagreement between the legs (should be ~0)
* `*`: passes your entry rules

Run it a few times a day for a few days to get a feel for it.

## 5. Build funding history

Coinbase has no API for past funding, so history comes from two places:

**(a) Start recording now, and leave it running** (cheap: one API call every 10 min):

```bash
$ mkdir -p logs
$ fcap -c config.yaml --log-file logs/record.log record
```

Run it in `tmux` (see step 7) or on a server so it keeps going. It stores each hourly
settlement under `data/coinbase/funding/`.

**(b) Import Coinbase's downloadable history.** Coinbase's derivatives funding-rate page
(coinbase.com → Derivatives → funding rates / market data) offers recent hourly history
(~90 days) as a CSV. For each perp you care about:

```bash
$ fcap -c config.yaml import-funding --venue coinbase --symbol BIP-20DEC30-CDE \
      --file ~/Downloads/BIP_funding.csv
```

The importer guesses the time and rate columns. If a column name doesn't contain "time"
or "rate", pass `--time-col` / `--rate-col`. It handles `%` values. Check the printed
first/last rows: rates should look like `2e-05` (hourly fractions), **not** `0.002`
(percent). If they're percents without a `%` sign, re-run with `--percent`. List the product
ids (BIP-…, ETP-…, DOP-…) and what one contract is worth with:

```bash
$ fcap -c config.yaml markets --venue coinbase --kind perp
```

The date part of an id (`-20DEC30-`) changes when Coinbase lists a successor contract.

## 6. Backtest and tune

```bash
$ fcap -c config.yaml backtest --trades trades.csv
$ fcap -c config.yaml sweep
```

* `backtest` prints net P&L, **APR on capital**, the funding vs. fees split, win rate,
  average hold, how much capital was working, max drawdown, and a per-coin breakdown.
  `trades.csv` lists every simulated trade.
* `sweep` tries a grid of entry/exit/horizon/forecast settings. It picks on the first
  half of the data and reports the second half (`test_apr_pct`). Use settings that are
  good **and stable** in the test column; avoid the single best training row. Copy them
  into `config.yaml` under `strategy:`.

Limitations, with ~90 days of data: few trades, so treat results as rough. The backtest
also ignores Coinbase's per-contract minimum fee (the scanner and engine include it), the
price gap between legs, and whether maker orders fill.

**Go / no-go:** continue only if the test-half APR on capital clearly beats parking cash
in a money-market/USDC yield, plus a premium for exchange risk (~10% APR above it).

## 7. Paper trade (2–4 weeks)

Paper mode uses live prices and funding with simulated fills, at the fees in your
config. It needs no keys and places no orders.

```bash
$ tmux new -s paper                        # keeps running after you close the terminal
$ source .venv/bin/activate
$ fcap -c config.yaml --log-file logs/paper.log paper
# detach: Ctrl-b then d      re-attach later: tmux attach -t paper
```

(`brew install tmux` if needed. On a laptop, prevent sleep, e.g. run it under
`caffeinate -i` on macOS.)

From another terminal (venv active):

```bash
$ fcap -c config.yaml status               # open pairs, funding earned, fees, closed P&L
$ tail -f logs/paper.log                   # live log; Ctrl-c stops watching
```

State lives in `state/paper/`, so a restart resumes where it left off. Delete that folder
to start over.

**Kill switches** (paper uses `state/paper/`, live uses `state/live/`):

```bash
$ touch state/paper/STOP        # no new pairs; existing ones are still managed
$ rm state/paper/STOP           # resume
$ touch state/paper/FLATTEN     # close everything now and stop opening
```

**After 2–4 weeks, check:**

* realised funding per pair vs. what `scan` expected when it opened
* fees per round trip vs. config
* `grep "timed out" logs/paper.log`: how often the spot maker order didn't fill in time.
  Paper fills are optimistic (no queue), so real maker fills will be fewer and slower.
* any `risk CLOSE` or `CRITICAL` lines, and why

## 8. Accounts and API keys

Use money and accounts you can dedicate to the bot. It assumes it owns every position on
the instruments it trades, and it will close a hedge leg it thinks is orphaned.

**Coinbase**

1. In the Coinbase app, complete **futures (derivatives) onboarding**. Until then, perp
   orders fail with HTTP 403.
2. Optional but wise: Advanced → Portfolios → create a portfolio for the bot's spot
   coins. Note that **US futures only trade from the Default portfolio**.
3. Go to portal.cdp.coinbase.com → API keys → **Create Secret API key**:
   * portfolio: **Default** (needed for futures)
   * permissions: **View** + **Trade** only, never Transfer
   * IP allowlist: your machine's public IP
   * signature algorithm: **ECDSA** (best tested with ccxt)
4. Save the downloaded key. The key name looks like `organizations/…/apiKeys/…`; the
   private key is a PEM block. Store the PEM as a file:

```bash
$ mkdir -p ~/.fcap && chmod 700 ~/.fcap
# paste the PEM (-----BEGIN EC PRIVATE KEY----- …) into ~/.fcap/coinbase.pem
$ chmod 600 ~/.fcap/coinbase.pem
```

**Kraken**

1. Settings → API → **Create key**, used only by this bot (a key shared with another tool
   causes nonce errors).
2. Permissions: **Query Funds**, **Query Open Orders & Trades**, **Query Closed Orders &
   Trades**, **Create & Modify Orders**, **Cancel/Close Orders**. **Not** Withdraw Funds.
3. Do **not** set a 2FA password on the key (ccxt can't send it). Use the IP allowlist
   instead. Setting a nonce window of ~10 s helps.
4. Optional: turn off auto-earn for coins the bot trades, so balances stay simple.

**Put the keys in `.env`** (git-ignored, in the project folder):

```bash
$ cat > .env <<'EOF'
COINBASE_API_KEY=organizations/your-org-id/apiKeys/your-key-id
COINBASE_SECRET_FILE=~/.fcap/coinbase.pem
KRAKEN_API_KEY=your-kraken-key
KRAKEN_SECRET=your-kraken-private-key
EOF
$ chmod 600 .env
$ fcap -c config.yaml check
```

Now each venue should also show `ok balance: …`, `ok your spot fee: maker x% / taker y%`
(**copy those numbers into `fees:` in config.yaml**) and, for Coinbase,
`ok positions: none` and a `margin buffer` line. Re-run `scan` afterwards, because your
real fees change which pairs pass.

## 9. Go live, small

**Fund the accounts.** The bot never moves money between venues; you do that by hand.

* **Kraken:** USD for spot buys (if a coin's spot leg is there).
* **Coinbase:** USD for spot buys, plus USD for futures margin. Coinbase moves USD from
  spot into the futures (CFM) account automatically (around 5 pm ET) to cover margin;
  moving it back is a "sweep" in the app. Keep **at least the full perp notional** as
  cash on Coinbase: overnight margin on short alt perps is 30–95% of notional, and it
  rises at the 5 pm ET switch from intraday to overnight rates.

**First live run: one contract.** In `config.yaml`:

```yaml
mode: live
portfolio:
  max_positions: 1
  max_position_usd: 600          # pairs whose single contract is worth more are skipped
```

(`fcap -c config.yaml markets --venue coinbase --kind perp` shows `$/contract`; it ranges
from ~$55 for 1000SHIB to ~$1,000+ for BTC and XRP.)

```bash
$ tmux new -s live
$ source .venv/bin/activate
$ fcap -c config.yaml --log-file logs/live.log live --i-understand-this-trades-real-money
```

Watch the first open and close **in both exchanges' UIs**:

* spot quantity bought == perp contracts × contract size (e.g. 1 DOGE contract = 5,000 DOGE)
* the fees charged match `fees:` in your config
* `fcap -c config.yaml status --live` agrees with the exchanges
* funding shows up on Coinbase hourly. The bot's funding figure for Coinbase is an
  *estimate* from published rates, because Coinbase has no funding-payment API. Compare
  it with your Coinbase statements weekly.

Then scale up gradually: `max_positions`, `max_position_usd`, `capital_usd`.

If the bot crashes or you stop it while a maker order is resting, that order stays on
the exchange. Check open orders on Coinbase and Kraken whenever you restart.
(`dead_man_seconds` in config makes Kraken cancel all open orders automatically if the
bot stops. Only use it if the bot is the only thing trading that account.)

## 10. Daily operations checklist

* `fcap -c config.yaml status --live`: anything stuck, partially closed, or unexpectedly old?
* `grep -E "CRITICAL|risk CLOSE|FAIL|HALTED" logs/live.log`: investigate every hit.
* Coinbase margin: `fcap -c config.yaml check` prints the account's distance to
  liquidation. The bot warns at 35% and closes pairs at 20%; add USD well before that.
* Venue announcements: new/delisted perps, maintenance (Coinbase futures pause Fridays
  5–6 pm ET), and contract roll-overs.
* Weekly: compare the bot's positions and funding with both exchanges' statements.

## Taxes (not advice)

Spot sells are capital-gain events; Coinbase and Kraken issue 1099s. Coinbase's
perpetual-style futures are regulated futures and may get Section 1256 (60/40)
treatment, but a spot long hedged with a short future can be a "mixed straddle" with
loss-deferral rules, and how funding payments are characterised is unsettled. Talk to a
CPA before you scale up.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `FAIL markets: NetworkError` | internet/VPN/firewall, or the venue is down; retry |
| `FAIL balance: AuthenticationError` | wrong key/secret, missing permission, IP not allowlisted, or (Coinbase) PEM not found: check `COINBASE_SECRET_FILE` |
| Kraken `EAPI:Invalid nonce` | key used by another tool, or clock off: use a fresh key, sync your clock |
| Coinbase order `403` / "onboarded users" | finish futures onboarding in the Coinbase app |
| `coinbase rejected sor_limit_ioc …; retrying as FOK` | expected once if Coinbase doesn't accept IOC on futures; the bot switches automatically |
| `scan` shows nothing with `*` | thresholds too strict for current funding, or fees too high: try `--all`, check `fees:` |
| `ENTRIES HALTED: stale market data` | a venue stopped answering; clears when data returns |
| `ENTRIES HALTED: unhedged residual …` | an execution left a one-sided remainder: fix it by hand on the exchange, then restart |
| frequent `maker order … timed out` | spot leg not filling: raise `execution.maker_timeout_s`, or accept that coin is too thin |

## Command reference

```
fcap -c config.yaml check                       connectivity, market data, keys, your fees
fcap -c config.yaml markets --venue V [--kind perp|spot]   symbols and contract sizes
fcap -c config.yaml scan [--top N] [--all]      live opportunities
fcap -c config.yaml record [--interval S]       log funding forward (Coinbase has no history API)
fcap -c config.yaml import-funding --venue V --symbol S --file F [--percent]
fcap -c config.yaml fetch [--days N] [--venues a,b]   download history (venues with an API)
fcap -c config.yaml backtest [--start YYYY-MM-DD] [--end …] [--trades file.csv]
fcap -c config.yaml sweep [--split 0.5] [--top N]
fcap -c config.yaml paper
fcap -c config.yaml live --i-understand-this-trades-real-money
fcap -c config.yaml status [--live] [--last N]
global options: -v (debug)  --log-file FILE  --env-file FILE (default .env)
```
