# Runbook: funding-capture from the US, step by step

Everything runs **on your own computer** (or a small always-on server). The cloud
sandbox this was built in can't reach exchanges, so the bot has not yet talked to a real
exchange. The plan below finds problems with no money at risk first.

**How to read this:** lines starting with `$` are commands. Type or paste them into
Terminal **without** the `$`, then press Enter. "You should see" describes the expected
output. If you see something else, copy the whole Terminal output into the Claude chat
and ask what to do.

**The plan at a glance**

| Phase | When | Time | Money at risk |
|---|---|---|---|
| 1. Install, look around, start recording + paper trading | today | ~45 min | none |
| 2. Let it run; check daily; backtest; decide | next 2–4 weeks | 5 min/day | none |
| 3. Exchange accounts and API keys | after a "go" in phase 2 | ~45 min | none |
| 4. One live contract, then scale slowly | the week after | a few days | small |

What the bot trades (US-legal venues only): **buy a coin spot on Kraken or Coinbase,
short the same coin's Coinbase US perpetual-style future, and collect the hourly funding
while it is positive.** Section [Background](#background-what-you-can-trade-from-the-us)
at the end explains why Hyperliquid and Lighter are not used and what the economics look
like.

---

## Phase 1: today

### Step 1. Open Terminal

macOS: press **⌘ + Space**, type `Terminal`, press Enter. Keep this window open for
the rest of Phase 1.

*Windows:* first install WSL. Open **PowerShell as Administrator**, run `wsl --install`,
restart, then open the **Ubuntu** app from the Start menu. Use the Ubuntu window as your
Terminal. Everywhere below, run `sudo apt update && sudo apt install -y python3.12
python3.12-venv git tmux gh` instead of the `brew` lines.

### Step 2. Install the tools (once)

1. Install Homebrew (the macOS package manager), if `brew --version` says "command not
   found":

   ```
   $ /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

   It asks for your Mac password (nothing shows while typing; that's normal). When it
   finishes it prints **"Next steps"** with two `echo …` / `eval …` lines: copy and run
   those two lines.

2. Install Python 3.12, GitHub's command-line tool, and tmux (keeps programs running
   after you close the window):

   ```
   $ brew install python@3.12 gh tmux
   ```

3. Check:

   ```
   $ python3.12 --version
   ```

   You should see `Python 3.12.x`.

### Step 3. Download the code

1. Log in to GitHub (the repository is yours and may be private):

   ```
   $ gh auth login
   ```

   Choose: **GitHub.com** → **HTTPS** → **Yes** (authenticate Git) → **Login with a web
   browser**. Copy the one-time code it shows, press Enter, paste the code in the
   browser page and approve.

2. Download the project into your home folder and switch to the branch with this work:

   ```
   $ cd ~
   $ gh repo clone Jason-82/funding-capture
   $ cd funding-capture
   $ git checkout claude/busy-maxwell-hiuy46
   ```

   You should see `Switched to a new branch 'claude/busy-maxwell-hiuy46'`.

### Step 4. Install the bot

```
$ python3.12 -m venv .venv
$ source .venv/bin/activate
$ pip install -e ".[dev]"
$ pytest -q
```

* After `source …` your prompt starts with `(.venv)`. **Every time you open a new
  Terminal window for this project, run these two lines first:**
  `cd ~/funding-capture` and `source .venv/bin/activate`.
* `pip install` prints a lot and ends with `Successfully installed …`.
* `pytest -q` should end with **`96 passed`** (the number may be higher after updates).

### Step 5. Create your settings file

```
$ cp config.us.yaml config.yaml
$ mkdir -p logs
```

You don't need to change anything yet. `config.yaml` is yours (git never uploads it).

### Step 6. Check that both exchanges answer (no keys needed)

```
$ fcap -c config.yaml check
```

You should see something like this (numbers will differ):

```
coinbase  (ccxt id: coinbase; credentials: none)
  ok    markets: 22 perps, 350 spot markets
  ok    funding: BTC +0.000004/1h, ETH +0.000011/1h, SOL +0.000020/1h
  ok    quotes: BIP-20DEC30-CDE 108000/108005, …
  skip  account checks (no API credentials in the environment)
kraken  (ccxt id: kraken; credentials: none)
  ok    markets: 0 perps, 600 spot markets
  ok    quotes: …
  skip  account checks (no API credentials in the environment)
```

* **About 20 perps on Coinbase** is the important part. `0 perps` means Coinbase
  changed its product list: paste the output to Claude.
* Funding like `+0.000020/1h` is per hour: × 8,760 ≈ 17.5% a year.
* Any `FAIL` line: retry once; if it persists, paste it to Claude.

### Step 7. See what's available right now

```
$ fcap -c config.yaml markets --venue coinbase --kind perp
$ fcap -c config.yaml scan --top 30 --all
```

`markets` lists each Coinbase perp's id (like `DOP-20DEC30-CDE` for DOGE), how many coins
one contract is, and what one contract is worth in dollars.

`scan` ranks every Kraken-or-Coinbase-spot + Coinbase-perp pair. Columns:

| Column | Meaning |
|---|---|
| `gross now` | funding right now, as a yearly rate on the position's size |
| `fcst` | what the bot expects funding to average over a month (it fades) |
| `net/cap` | **expected yearly return on the money it ties up, after all fees**: the number that matters |
| `b/e h` | hours the pair must be held to earn back its fees |
| `vol $M` | 24h trading volume (the smaller leg), in $ millions |
| `ok` | `*` = passes your entry rules today |

Don't worry if nothing has a `*`; funding moves with the market. This is just to get a
feel for it.

### Step 8. Start recording funding and paper trading (leave them running)

Coinbase doesn't publish funding history through its API, so the bot records it from
now on. At the same time it paper trades: real prices, real funding, **simulated**
orders. No keys, no money.

1. Start the recorder in its own background session:

   ```
   $ tmux new -s record
   ```

   The window clears (you're now "inside" tmux). Run:

   ```
   $ cd ~/funding-capture && source .venv/bin/activate
   $ fcap -c config.yaml --log-file logs/record.log record
   ```

   You should see `recorded 22 funding readings` (about every 10 minutes). Now
   **detach**: press **Ctrl + b**, let go, then press **d**. You're back in your normal
   window and the recorder keeps running.

2. Start paper trading the same way:

   ```
   $ tmux new -s paper
   $ cd ~/funding-capture && source .venv/bin/activate
   $ fcap -c config.yaml --log-file logs/paper.log paper
   ```

   You should see `bootstrapped: … instruments, … candidates, 0 open positions`, then
   every minute a `best: spot_perp …` line and `open positions: …`. Detach with
   **Ctrl + b**, then **d**.

3. Keep the Mac awake while these run (plugged in, lid open):

   ```
   $ tmux new -d -s awake 'caffeinate -dims'
   ```

To look at a session later: `tmux attach -t paper` (or `record`). Detach again with
Ctrl + b, d. To list sessions: `tmux ls`. If you restart the Mac, repeat step 8. The bot
picks up where it left off.

**Phase 1 done.**

---

## Phase 2: the next 2–4 weeks

### Every day (5 minutes)

Open Terminal, then:

```
$ cd ~/funding-capture && source .venv/bin/activate
$ tmux ls                                    # should list: awake, paper, record
$ fcap -c config.yaml status                 # paper positions and results
$ grep -ciE "critical|error|halted" logs/paper.log   # ideally 0
```

`status` shows open pairs (coin, venues, size, funding earned, fees), closed pairs with
their net result, and once a day has passed:

```
running for 9.2 days on $5,000 of capital
annualised return on capital so far: +12.4%  (noisy until a few weeks have passed)
```

If `tmux ls` is missing `paper` or `record`, restart it as in step 8. If the error count
grows, run `grep -iE "critical|error|halted" logs/paper.log | tail -20` and paste the
output to Claude.

### After 2 weeks: first backtest on the recorded data

```
$ fcap -c config.yaml backtest --trades trades.csv
```

This replays the recorded funding with your settings. Read: `net pnl`, **`APR on
capital`**, `funding / costs`, number of trades. `trades.csv` opens in Numbers or Excel.
Two weeks is short, so treat it as a sanity check, not a verdict.

### After 4 weeks: tune and decide

```
$ fcap -c config.yaml sweep
```

This tries many settings: it picks on the first half of the data and scores on the
second half (`test_apr_pct`). Paste the output to Claude if you want help choosing.
Change settings with `nano config.yaml`: arrow keys to move, edit, **Ctrl + O** then
Enter to save, **Ctrl + X** to exit. The paper engine reads settings at start, so
restart it (attach, **Ctrl + c**, run the `fcap … paper` line again, detach).

**Decision rule** (from `status` and the backtest):

| Paper result, annualised on capital | What to do |
|---|---|
| above ~15% and the backtest agrees | go to Phase 3 |
| 5–15% | keep paper trading; consider better fee tiers (see Background) |
| below ~5% or negative | stop here; a savings account beats it with less risk |

Paper fills of the spot leg are a bit optimistic (real maker orders queue), so live will
usually come in lower than paper.

---

## Phase 3: exchange accounts and API keys

Only after a "go". Still no money moves in this phase.

### 3a. Coinbase: turn on futures

1. On coinbase.com (or the app), open **Advanced** trading, find **Futures**
   (sometimes labelled Derivatives) and choose **Get started** / **Enable**. Answer the
   questionnaire and accept the agreements. Approval can take a little while.
2. US futures can only trade from your **Default** portfolio. Use that one for the bot.

### 3b. Coinbase: create the API key

1. Find your public IP address (used to lock the key to your connection):

   ```
   $ curl -s https://api.ipify.org; echo
   ```

2. Go to **portal.cdp.coinbase.com** and sign in with your Coinbase account. Open
   **API keys** → **Secret API keys** → **Create API key**.
3. Settings:
   * Nickname: `funding-bot`
   * Portfolio: **Default**
   * Permissions: **View** ✅ **Trade** ✅ **Transfer** ❌ (leave off)
   * IP allowlist: the address from step 1. If your home IP changes often, you can leave
     this empty; the key still can't move money.
   * Advanced settings → signature algorithm: **ECDSA**
4. Create. A file named like `cdp_api_key.json` downloads. Store it in the bot, then
   delete the download:

   ```
   $ cd ~/funding-capture && source .venv/bin/activate
   $ fcap keys coinbase --json ~/Downloads/cdp_api_key.json
   $ rm ~/Downloads/cdp_api_key.json
   ```

   You should see `saved COINBASE_API_KEY, COINBASE_SECRET_FILE to .env`.

### 3c. Kraken: create the API key

1. On kraken.com, sign in, click your profile icon → **Settings** → **API** (Kraken Pro:
   **Settings → API**) → **Create API key**.
2. Settings:
   * Name: `funding-bot`
   * Funds: **Query** ✅, Deposit ❌, **Withdraw ❌**
   * Orders and trades: **Query open orders & trades** ✅, **Query closed orders &
     trades** ✅, **Create & modify orders** ✅, **Cancel & close orders** ✅
   * Everything else off. **No 2FA password on the key** (the bot can't type one).
   * Advanced: nonce window `10` seconds; IP restriction: same as Coinbase (optional).
3. **Generate key.** Kraken shows an **API Key** and a **Private Key** once. Keep the
   page open and run:

   ```
   $ fcap keys kraken
   ```

   Paste the API key when asked, then the private key (it stays invisible while you
   paste; that's normal), Enter after each.

### 3d. Check the keys and set your real fees

```
$ fcap -c config.yaml check
```

Each venue now also shows `ok balance: …` and a fee line like:

```
  ok    your spot fee: spot_maker: 0.0040  spot_taker: 0.0080  (fractions; paste under fees: kraken:)
```

Open the config and put those numbers in under `fees:` for that exchange:

```
$ nano config.yaml
```

```yaml
fees:
  coinbase:
    spot_taker: 0.0090      # ← from the coinbase line
    spot_maker: 0.0050      # ←
    …
  kraken:
    spot_taker: 0.0080      # ← from the kraken line
    spot_maker: 0.0040      # ←
```

Save (Ctrl + O, Enter) and exit (Ctrl + X). Then re-run `fcap -c config.yaml scan`,
because your real fees change which pairs pass. Restart the paper engine so it uses
them too.

---

## Phase 4: one live contract, then scale

### 4a. Fund the accounts

For the first trial the bot opens **one** pair of at most $600:

* **Kraken:** deposit about **$800 USD** (Funding → Deposit → USD → bank/ACH). It buys
  the spot coin with this.
* **Coinbase:** keep about **$800 USD** in the **Default** portfolio. Futures margin is
  drawn from that USD balance (Coinbase transfers it to the futures account itself; its
  futures help pages describe the timing). Overnight margin on alt-coin shorts can reach
  30–95% of their value, so keep the full amount there. Some pairs use Coinbase spot
  instead of Kraken; this covers those too.

### 4b. Switch the config to live, sized to one contract

```
$ nano config.yaml
```

**Change these existing lines** (don't add a second `portfolio:` section; the bot
refuses duplicate sections):

```yaml
mode: live                      # was: paper
portfolio:
  capital_usd: 1600             # was: 5000 (what you actually deposited)
  max_positions: 1              # was: 4
  max_position_usd: 600         # was: 1000
```

### 4c. Start it

```
$ tmux new -s live
$ cd ~/funding-capture && source .venv/bin/activate
$ fcap -c config.yaml --log-file logs/live.log live --i-understand-this-trades-real-money
```

Detach (Ctrl + b, d). It opens a pair only when one passes your rules. That may take
hours or days.

### 4d. Watch the first trade closely

When `fcap -c config.yaml status --live` shows an open pair:

1. **Kraken** (or Coinbase spot): the coin balance went up by the `qty` shown.
2. **Coinbase → Futures → Positions:** a short in that coin of *contracts × contract
   size* = the same `qty` (e.g. 1 DOGE contract = 5,000 DOGE).
3. The fees charged on both orders match your `fees:` settings.
4. Over the next hours, Coinbase shows hourly funding credits on the futures position.
   The bot's `funding=` figure for Coinbase is an **estimate** (Coinbase has no API for
   funding paid), so compare the two after a few days.

Let it close the pair by itself (when funding fades), or test closing with
`touch state/live/FLATTEN` (it closes everything; `rm state/live/FLATTEN` lets it trade
again). After
the close, both exchanges should be back to no position and only fees + funding should
have changed your balances.

If anything doesn't match, stop (see below) and paste `fcap -c config.yaml status --live`
plus `tail -50 logs/live.log` to Claude.

### 4e. Scale up slowly

After a couple of clean round trips, raise `max_positions` (2, then 4) and
`max_position_usd`, and keep `capital_usd` equal to what you actually deposited. Restart
the live engine after each change.

### Stopping, pausing, emergencies

| You want to | Do this |
|---|---|
| pause new trades (keep managing open ones) | `touch state/live/STOP` (resume: `rm state/live/STOP`) |
| close everything now | `touch state/live/FLATTEN` |
| stop the program | `touch state/live/STOP`, wait 2 minutes, `tmux attach -t live`, **Ctrl + c**. Open pairs stay open and are managed again when you restart |
| `status` says `ENTRIES HALTED` | the bot hit something it won't guess about (e.g. an order it couldn't confirm). Stop it, check open orders and positions on both exchanges, fix by hand, restart |

### Daily routine once live

```
$ fcap -c config.yaml status --live
$ fcap -c config.yaml check                        # Coinbase "margin buffer" line
$ grep -iE "critical|error|halted|still open" logs/live.log | tail
```

* Margin buffer: the bot warns at 35% and closes pairs at 20%. Add USD to Coinbase well
  before that.
* Weekly: compare the bot's positions and funding with both exchanges' statements.

---

## Background: what you can trade from the US

| Venue | What the bot uses it for | Status for a US person |
|---|---|---|
| **Coinbase** (Advanced) | spot long legs **and** the short perp leg: CFTC-regulated perpetual-style futures on ~20 coins (BTC, ETH, SOL, XRP, DOGE, ADA, LINK, AVAX, LTC, DOT, XLM, HBAR, SUI, BCH, NEAR, AAVE, ONDO, ENA, ZEC, 1000SHIB, 1000PEPE), hourly funding | ✅ after futures onboarding |
| **Kraken Pro** | spot long legs (many alts) | ✅ spot. Not available in NY or ME; no crypto withdrawals from IN |
| Kraken US perps (Bitnomial) | – | exist, but no retail API |
| Kalshi | – | lists a few perps since 2026, but no library support and invite-only API access |
| Hyperliquid, Lighter | – | ❌ their terms of use exclude US persons |

**Economics, honestly:**

* **Spot fees are high.** Coinbase's entry tier is 0.50% maker / 0.90% taker; Kraken's is
  about 0.40% / 0.80%. The bot rests limit (maker) orders on the spot leg, but a round
  trip still costs ~1%. It therefore plans to hold positions about a month.
* **Capital efficiency is poor.** You pay for the spot coins in full and Coinbase holds a
  large margin on the short, so a $1,000 pair ties up about $2,000.
* **Better tiers change everything.** Kraken lowers fees with assets held on the account
  (~$20k → ~0.22% maker); Coinbase lowers them with volume or a USDC balance.

## Taxes (not advice)

Spot sales are capital-gain events (Coinbase and Kraken send 1099s). Coinbase's US
perpetual-style futures are regulated futures and may get Section 1256 (60/40)
treatment. A spot long hedged with a short future can be a "mixed straddle" with
loss-deferral rules, and how funding is taxed is unsettled. Talk to a CPA before
scaling up.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `command not found: fcap` | you're in a new window: `cd ~/funding-capture && source .venv/bin/activate` |
| `FAIL markets: NetworkError` | internet, VPN or firewall, or the exchange is down: retry |
| `FAIL credentials: COINBASE_SECRET_FILE: cannot read …` | re-run `fcap keys coinbase --json <file>` with a freshly created key |
| `FAIL balance: AuthenticationError` | wrong key, missing permission, or your IP changed (update the allowlist) |
| Kraken `EAPI:Invalid nonce` | the key was used by another program, or your clock is off: make a new key just for the bot |
| Coinbase order `403` / "onboarded users" | finish futures onboarding (3a) |
| `duplicate key 'portfolio'` | you pasted a second section into config.yaml: merge them into one |
| `fees.kraken.spot_maker … fractions` | write `0.004`, not `0.4` or `0.4%` |
| `found state/engine.json from an older version` | move it to `state/paper/` or `state/live/` as the message says |
| `scan` never shows `*` | normal in quiet markets; check your `fees:` are right |
| many `maker order … timed out` in the log | spot orders aren't filling: raise `execution.maker_timeout_s` a little, or that coin is too thin |

## Command reference

```
fcap -c config.yaml check                       connectivity, market data, keys, your fees
fcap keys coinbase --json FILE | fcap keys kraken    store API keys in .env
fcap -c config.yaml markets --venue V [--kind perp|spot]   symbols and contract sizes
fcap -c config.yaml scan [--top N] [--all]      live opportunities
fcap -c config.yaml record                      log funding forward (Coinbase has no history API)
fcap -c config.yaml import-funding --venue V --symbol S --file F [--percent]
fcap -c config.yaml backtest [--trades file.csv]
fcap -c config.yaml sweep
fcap -c config.yaml paper
fcap -c config.yaml live --i-understand-this-trades-real-money
fcap -c config.yaml status [--live]
global options: -v (debug)  --log-file FILE  --env-file FILE (default .env)
```
