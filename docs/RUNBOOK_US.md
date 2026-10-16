# Runbook: funding-capture from the US, on Windows with Git Bash

Everything runs **on your own Windows PC in Git Bash**. The plan below finds problems
with no money at risk first. The Phase 1 commands are also run automatically on a
Windows machine in Git Bash against the real Coinbase and Kraken APIs (the
`windows-runbook` job in `.github/workflows/ci.yml`).

**How to read this**

* Lines starting with `$` are commands. Type or paste them into Git Bash **without** the
  `$`, then press Enter.
* **Pasting into Git Bash:** **Shift + Insert**, or right-click → **Paste**. Ctrl + V
  usually does nothing there.
* **Copying out of Git Bash:** select the text with the mouse. That copies it.
* "You should see" describes the expected output. If you see something else, copy the
  whole output into the Claude chat and ask what to do.

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

### Step 1. Install the tools (once)

1. **Git Bash.** If it's in your Start menu, skip this. Otherwise download **Git for
   Windows** from <https://git-scm.com/download/win>, run the installer and accept every
   default. The defaults include Git Credential Manager, which step 2 uses to sign in to
   GitHub.

2. Open **Git Bash**: press the **Windows key**, type `Git Bash`, press Enter.

3. **Python 3.12.** In Git Bash:

   ```
   $ winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
   ```

   Click **Yes** if Windows asks whether the installer may make changes. If you get
   `winget: command not found`, use the website instead: on
   <https://www.python.org/downloads/windows/> download the **Windows installer
   (64-bit)** of the latest 3.12 release, run it, tick **Add python.exe to PATH** on the
   first screen, then **Install Now**.

4. **Close Git Bash and open a new one** so it sees the new program. Check:

   ```
   $ py -3.12 --version
   ```

   You should see `Python 3.12.x`. Type `py -3.12` as written, not `python`: until
   step 3 is done, `python` on Windows may open the Microsoft Store instead.

5. **Set the clock to sync automatically.** Exchanges reject requests from a PC whose
   clock is off. Open **Settings → Time & language → Date & time**, turn on **Set time
   automatically**, then click **Sync now**.

### Step 2. Download the code

```
$ cd ~
$ git clone https://github.com/Jason-82/funding-capture.git
$ cd funding-capture
$ git checkout claude/busy-maxwell-hiuy46
```

* At the `git clone` line a **Connect to GitHub** window may open (the repository may
  be private). Choose **Sign in with your browser**, approve in the browser, and the
  download continues.
* `~` is your Windows user folder, so the project lands in
  `C:\Users\<you>\funding-capture`. Leave it there: don't move it into OneDrive,
  Dropbox or another synced folder (it will hold your API keys).
* The last line should say `Switched to a new branch 'claude/busy-maxwell-hiuy46'`.

### Step 3. Install the bot

```
$ py -3.12 -m venv .venv
$ source .venv/Scripts/activate
$ pip install -e ".[dev]"
$ pytest -q
```

* After `source …` your prompt shows `(.venv)`. On Windows the folder is `Scripts`,
  not `bin`.
* `pip install` prints a lot and ends with `Successfully installed …`. A notice that a
  new pip release is available is harmless.
* `pytest -q` should end with **`98 passed`** (the number may be higher after updates).

**Make a shortcut.** Every new Git Bash window has to switch to the project and activate
it. Save that as the command `bot`:

```
$ echo "alias bot='cd ~/funding-capture && source .venv/Scripts/activate'" >> ~/.bashrc
```

From now on, **type `bot` first in every new Git Bash window.** (The first new window may
print `WARNING: Found ~/.bashrc but no ~/.bash_profile …` and fix that itself. That's
fine.)

### Step 4. Create your settings file

```
$ cp config.us.yaml config.yaml
$ mkdir -p logs
```

You don't need to change anything yet. `config.yaml` is yours (git never uploads it).
When you do need to edit it later: `notepad config.yaml`, change it, **Ctrl + S**,
close Notepad. Notepad is fine; the bot copes with the way it saves files.

### Step 5. Check that both exchanges answer (no keys needed)

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

### Step 6. See what's available right now

Maximise the Git Bash window first; the tables are wide.

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

### Step 7. Start recording funding and paper trading (leave them running)

Coinbase doesn't publish funding history through its API, so the bot records it from
now on. At the same time it paper trades: real prices, real funding, **simulated**
orders. No keys, no money.

Each of the two programs runs in **its own Git Bash window, which you leave open**.
Minimise it, but don't close it: closing the window stops the program.

1. **Recorder.** Open a new Git Bash window (Windows key → `Git Bash`), then:

   ```
   $ bot
   $ printf '\e]0;RECORD\a'
   $ fcap -c config.yaml --log-file logs/record.log record
   ```

   The `printf` line names the window **RECORD**, so you can tell the windows apart in
   the taskbar. You should see `recorded 22 funding readings`, then another such line
   about every 10 minutes. Minimise the window.

2. **Paper trading.** Open another new Git Bash window:

   ```
   $ bot
   $ printf '\e]0;PAPER\a'
   $ fcap -c config.yaml --log-file logs/paper.log paper
   ```

   You should see `bootstrapped: … instruments, … candidates, 0 open positions`, then
   every minute a `best: spot_perp …` line and `open positions: …`. Minimise the window.

3. **Keep the PC awake while these run.**
   * **Settings → System → Power & battery** (desktops: **Power**) → **Screen and
     sleep** → set **"When plugged in, put my device to sleep after"** to **Never**. The
     screen may still turn off; that's fine.
   * Laptop: keep it plugged in. If you close the lid, first set **Control Panel →
     Power Options → Choose what closing the lid does → When plugged in: Do nothing**.
   * Windows Update may restart the PC overnight. **Settings → Windows Update →
     Advanced options → Active hours** sets when it won't.

**After a restart** (or if you closed a window by accident), repeat step 7. The bot
picks up where it left off.

**To stop the paper engine cleanly** (for example to change settings), in any Git Bash
window type `bot`, then `touch state/paper/QUIT`. Within a minute the PAPER window
prints `QUIT file found: exiting…` and returns to the `$` prompt. Avoid **Ctrl + C**:
in Git Bash it kills the program instantly, in the middle of whatever it is doing. (The
recorder doesn't matter: close its window any time.)

**Phase 1 done.**

---

## Phase 2: the next 2–4 weeks

### Every day (5 minutes)

Open Git Bash, then:

```
$ bot
$ tail -n 2 logs/paper.log        # time on the last line: within the last 2 minutes
$ tail -n 1 logs/record.log       # within the last 10 minutes
$ fcap -c config.yaml status      # paper positions and results
$ grep -ciE "critical|error|halted" logs/paper.log    # ideally 0
```

The log lines start with the date and time as your PC shows it. If a log has stopped
moving, that program isn't running: check its window, then restart it as in step 7.

`status` shows open pairs (coin, venues, size, funding earned, fees), closed pairs with
their net result, and once a day has passed:

```
running for 9.2 days on $5,000 of capital
annualised return on capital so far: +12.4%  (noisy until a few weeks have passed)
```

If the error count grows, run `grep -iE "critical|error|halted" logs/paper.log | tail -20`
and paste the output to Claude.

### After 2 weeks: first backtest on the recorded data

```
$ fcap -c config.yaml backtest --trades trades.csv
$ start trades.csv
```

This replays the recorded funding with your settings. Read: `net pnl`, **`APR on
capital`**, `funding / costs`, number of trades. `start trades.csv` opens the trade
list in Excel. Two weeks is short, so treat it as a sanity check, not a verdict.

### After 4 weeks: tune and decide

```
$ fcap -c config.yaml sweep
```

This tries many settings: it picks on the first half of the data and scores on the
second half (`test_apr_pct`). Paste the output to Claude if you want help choosing.

To change a setting: `notepad config.yaml`, edit, Ctrl + S, close Notepad. The paper
engine reads settings when it starts, so restart it: `touch state/paper/QUIT`, wait until
the PAPER window shows the `$` prompt again, then in that window press **↑** (brings back
the last command) and Enter.

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
4. Create. A file named like `cdp_api_key.json` downloads to your **Downloads**
   folder. Store it in the bot, then delete the download:

   ```
   $ bot
   $ ls ~/Downloads/*.json
   $ fcap keys coinbase --json ~/Downloads/cdp_api_key.json
   $ rm ~/Downloads/cdp_api_key.json
   ```

   `ls` shows the exact file name. If your browser named it differently (for example
   `cdp_api_key (1).json`), use that name in quotes in the next two lines:
   `--json "$HOME/Downloads/cdp_api_key (1).json"`.

   You should see `saved COINBASE_API_KEY, COINBASE_SECRET_FILE to .env`. The private
   key itself goes to `C:\Users\<you>\.fcap\coinbase.pem`.

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

   Copy the API key from the Kraken page, paste it (Shift + Insert), Enter. Then the
   private key, Enter. **Git Bash can't hide what you paste**, so the private key shows
   on screen. As soon as you see `saved KRAKEN_API_KEY, KRAKEN_SECRET to .env`, wipe
   the screen:

   ```
   $ clear
   ```

The keys now sit in `.env` in the project folder and in `C:\Users\<you>\.fcap`. Both
are inside your Windows user folder, which other accounts on the PC can't read, and git
never uploads them.

### 3d. Check the keys and set your real fees

```
$ fcap -c config.yaml check
```

Each venue now also shows `ok balance: …` and a fee line like:

```
  ok    your spot fee: spot_maker: 0.0040  spot_taker: 0.0080  (fractions; paste under fees: kraken:)
```

Open the config:

```
$ notepad config.yaml
```

Find the `fees:` section and replace the `spot_taker` and `spot_maker` numbers for each
exchange with the ones `check` printed for it. For example:

```yaml
fees:
  coinbase:
    spot_taker: 0.0090      # from the coinbase line
    spot_maker: 0.0050
    …
  kraken:
    spot_taker: 0.0080      # from the kraken line
    spot_maker: 0.0040
```

Save (Ctrl + S) and close Notepad. Then re-run `fcap -c config.yaml scan`, because your
real fees change which pairs pass. Restart the paper engine (see Phase 2) so it uses
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
$ notepad config.yaml
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

Save and close Notepad.

### 4c. Start it

Open a new Git Bash window:

```
$ bot
$ printf '\e]0;LIVE\a'
$ fcap -c config.yaml --log-file logs/live.log live --i-understand-this-trades-real-money
```

Minimise the window; never close it while the bot runs (see "Stopping" below). It opens
a pair only when one passes your rules. That may take hours or days. The paper engine
can keep running alongside for comparison; it keeps separate records.

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
again). After the close, both exchanges should be back to no position and only fees +
funding should have changed your balances.

If anything doesn't match, stop the bot (see below) and paste
`fcap -c config.yaml status --live` plus `tail -n 50 logs/live.log` to Claude.

### 4e. Scale up slowly

After a couple of clean round trips, raise `max_positions` (2, then 4) and
`max_position_usd`, and keep `capital_usd` equal to what you actually deposited. Restart
the live engine after each change (`touch state/live/QUIT`, wait for the `$` prompt in
the LIVE window, ↑, Enter).

### Stopping, pausing, emergencies

Run these from any Git Bash window after `bot`.

| You want to | Do this |
|---|---|
| pause new trades (keep managing open ones) | `touch state/live/STOP` (resume: `rm state/live/STOP`) |
| close everything now | `touch state/live/FLATTEN` |
| stop the program | `touch state/live/QUIT`. It exits within a minute, between steps, never halfway through an order; then close the window. Open pairs stay open and are managed again when you restart |
| restart the PC (e.g. Windows Update) | `touch state/live/QUIT` first, then restart, then start it again (4c) |
| `status` says `ENTRIES HALTED` | the bot hit something it won't guess about (e.g. an order it couldn't confirm). Stop it, check open orders and positions on both exchanges, fix by hand, restart |

**Don't stop the live bot with Ctrl + C or the window's ✕.** In Git Bash both kill it
instantly. If that happens while it is opening a pair, one leg can be left on an
exchange without the bot knowing about it. If it ever happens, compare both exchanges'
positions and open orders with `fcap -c config.yaml status --live` and close anything
the bot doesn't list, by hand, before restarting.

### Daily routine once live

```
$ bot
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
| `bash: fcap: command not found` | new window: type `bot` (or `cd ~/funding-capture && source .venv/Scripts/activate`) |
| `bash: bot: command not found` | run the `echo "alias bot=…"` line from step 3 once more, then close and reopen Git Bash |
| `bash: py: command not found` | Python isn't installed, or Git Bash was opened before installing it: close and reopen Git Bash; if it persists, reinstall from python.org with **Add python.exe to PATH** ticked |
| `python` opens the Microsoft Store | use `py -3.12` (needed only before step 3); or turn off **Settings → Apps → Advanced app settings → App execution aliases → python.exe / python3.exe** |
| `.venv/bin/activate: No such file or directory` | on Windows it's `.venv/Scripts/activate` |
| pasting does nothing | **Shift + Insert**, or right-click → Paste |
| `git clone` asks for a username/password in the window | the sign-in window didn't appear: reinstall Git for Windows with the default options (they include Git Credential Manager) and clone again |
| `FAIL markets: NetworkError` | internet, VPN or firewall, or the exchange is down: retry |
| `FAIL credentials: COINBASE_SECRET_FILE: cannot read …` | re-run `fcap keys coinbase --json <file>` with a freshly created key |
| `FAIL balance: AuthenticationError` | wrong key, missing permission, or your IP changed (update the allowlist) |
| Kraken `EAPI:Invalid nonce`, or Coinbase `401` | clock: **Settings → Time & language → Date & time → Sync now**. If it persists, the key was used by another program: make a new key just for the bot |
| Coinbase order `403` / "onboarded users" | finish futures onboarding (3a) |
| `PermissionError` naming `engine.json` | another program (Excel, an editor, a sync or backup tool) has the bot's state file open: close it. The bot retries for 2 seconds before giving up |
| `duplicate key 'portfolio'` | you pasted a second section into config.yaml: merge them into one |
| `fees.kraken.spot_maker … fractions` | write `0.004`, not `0.4` or `0.4%` |
| `found state/engine.json from an older version` | move it to `state/paper/` or `state/live/` as the message says |
| `scan` never shows `*` | normal in quiet markets; check your `fees:` are right |
| many `maker order … timed out` in the log | spot orders aren't filling: raise `execution.maker_timeout_s` a little, or that coin is too thin |
| a window shows nothing new for a long time | check the log (`tail -n 2 logs/paper.log`): if its last line is old too, the program died; start it again |

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

kill switches (paper engine: state/paper/, live engine: state/live/)
touch state/live/STOP       no new pairs            rm state/live/STOP to resume
touch state/live/FLATTEN    close all pairs         rm state/live/FLATTEN to trade again
touch state/live/QUIT       exit the program cleanly (the file removes itself)
```

## On a Mac or Linux instead

The same steps work in Terminal with these changes: install Python 3.12 with your
package manager and type `python3.12` where this says `py -3.12`; activate with
`source .venv/bin/activate`; run the long-lived programs under `tmux` (or
`nohup … &`) instead of in separate windows; keep a Mac awake with `caffeinate -dims`.
There, `fcap keys` hides the private key while you paste it.
