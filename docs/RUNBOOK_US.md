# Runbook: funding-capture from the US, on Windows with Git Bash

Everything runs **on your own Windows PC in Git Bash**. The plan below finds problems
with no money at risk first. The Phase 1 commands are also run automatically on a
Windows machine in Git Bash against the real Coinbase and Kraken APIs (the
`windows-runbook` job in `.github/workflows/ci.yml`).

**How to read this**

* Lines starting with `$` are commands. Type or paste them into Git Bash **without** the
  `$`, then press Enter.
* **Pasting into Git Bash:** **Shift + Insert**, or right-click → **Paste**. Don't press
  Ctrl + V there: it shows nothing but garbles whatever you type or paste next. If you
  did, press Backspace until the line is empty, then paste with Shift + Insert.
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
   `winget: command not found`, download
   <https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe> instead (3.12.10
   is the last 3.12 release with a Windows installer; newer 3.12 entries on python.org say
   "No files for this release", which is normal). Run it, tick **Add python.exe to PATH**
   on the first screen, then **Install Now**.

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
* `pytest -q` should end with **`163 passed`** (the number may be higher after updates)
  and no `failed`.

**Make a shortcut.** Every new Git Bash window has to switch to the project and activate
it. Save that as the command `bot`:

```
$ echo "alias bot='cd ~/funding-capture && source .venv/Scripts/activate && stty -ixon'" >> ~/.bashrc
```

From now on, **type `bot` first in every new Git Bash window.** (The first new window may
print `WARNING: Found ~/.bashrc but no ~/.bash_profile …` and fix that itself. That's
fine. The `stty -ixon` part stops Ctrl + S from freezing the window.)

**If a new window says `bash: bot: command not found`**, run these two lines, then close
and reopen Git Bash:

```
$ [ -e ~/.bash_profile ] || echo 'test -f ~/.profile && . ~/.profile' >> ~/.bash_profile
$ grep -qs bashrc ~/.bash_profile || echo 'test -f ~/.bashrc && . ~/.bashrc' >> ~/.bash_profile
```

If it still fails, run `grep "alias bot" ~/.bashrc`; only if that prints nothing, run
the `echo "alias bot=…"` line above again.

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
  ok    markets: 24 perps, 407 spot markets
  ok    funding: BTC +0.000015/1h, ETH +0.000006/1h, ZEC +0.000034/1h
  ok    quotes: BIP-20DEC30-CDE 81970/81980, ETP-20DEC30-CDE 2463/2464, …
  skip  account checks (no API credentials in the environment)
kraken  (ccxt id: kraken; credentials: none)
  ok    markets: 0 perps, 633 spot markets
  ok    quotes: 0G/USD 0.251/0.252, 1INCH/USD 0.0949/0.0951, 2Z/USD 0.03331/0.03332
  skip  account checks (no API credentials in the environment)
```

* **About 24 perps on Coinbase** is the important part (these are the US ones, with
  ids ending in `-CDE`). `0 perps` means Coinbase changed its product list: paste the
  output to Claude.
* Funding like `+0.000015/1h` is per hour: × 8,760 ≈ 13% a year.
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
   $ printf '\e]0;RECORD\a'; fcap -c config.yaml --log-file logs/record.log record
   ```

   The `printf …;` part names the window **RECORD** while the program runs. Hover over
   the Git Bash icon in the taskbar to see each window's name. You should see `recorded 24
   funding readings`, then another such line about every 10 minutes. Minimise the window.

2. **Paper trading.** Open another new Git Bash window:

   ```
   $ bot
   $ printf '\e]0;PAPER\a'; fcap -c config.yaml --log-file logs/paper.log paper
   ```

   You should see `bootstrapped: … instruments, … candidates, 0 open positions`, then
   every minute a `best: spot_perp …` line and `open positions: …`. Minimise the window.

3. **Keep the PC awake while these run.** Open a third Git Bash window and run:

   ```
   $ powercfg -change -standby-timeout-ac 0
   $ powercfg -change -hibernate-timeout-ac 0
   ```

   That means: when plugged in, never sleep and never hibernate. The screen may still
   turn off; that's fine. If either line prints an error, do it by hand: press the
   Windows key, type `sleep`, open **Power, sleep and battery settings** (Windows 10:
   **Power & sleep settings**) and set every **Plugged in** sleep and hibernate time to
   **Never**. Depending on your Windows version the setting is called **Make my device
   sleep after**, **When plugged in, put my device to sleep after** or **When plugged in,
   PC goes to sleep after**.
   * Laptop: keep it plugged in. To be able to close the lid, press the Windows key, type
     `lid`, open **Change what closing the lid does**, set **When I close the lid →
     Plugged in** to **Do nothing**, and click **Save changes**.

**After a restart of the PC** (or if you closed a window by accident), repeat step 7. The
bot picks up where it left off. Windows Update can restart the PC by itself at night;
Phase 4 explains what to do about that once real money is involved.

**To stop the paper engine cleanly** (for example to change settings), open a **new** Git
Bash window. Don't type into the PAPER window: while the bot runs there it ignores what
you type. In the new window type `bot`, then `touch state/paper/QUIT`. Usually within a
minute (longer if it is in the middle of a trade) the PAPER window prints
`QUIT file found: exiting…` and shows the `$` prompt again. Avoid **Ctrl + C**: in Git
Bash it kills the program instantly, in the middle of whatever it is doing. (The
recorder doesn't matter: close its window any time.)

**Phase 1 done.**

---

## Phase 2: the next 2–4 weeks

### Every day (5 minutes)

Open Git Bash, then:

```
$ bot
$ fcap -c config.yaml status                      # paper positions and results
$ grep "^20" logs/record.log | tail -n 1          # time at the start: within 10 minutes
$ grep -ciE "critical|error|halted" logs/paper.log    # ideally 0
```

The second line of `status` says `engine: running, last saved 0 min ago` (or 1, 2). If
it says `WARNING: the paper engine is NOT running`, start it again as in step 7. The
recorder line starts with the date and time; if that is more than 15 minutes old, close
the RECORD window (✕ is fine for the recorder) and start it again as in step 7.

`status` then shows open pairs (coin, venues, size, funding earned, fees), closed pairs
with their net result, and once a day has passed:

```
estimated cost to close the open pairs: $4.10 (counted below)
first started 9.2 days ago, on $5,000 of capital
annualised return on capital so far: +12.4%  (noisy until a few weeks have passed)
```

The return already subtracts what closing the open pairs would cost, so it is the number
to judge by.

If the error count grows, run `grep -iE "critical|error|halted" logs/paper.log | tail -20`
and paste the output to Claude.

### After 2 weeks: first backtest on the recorded data

```
$ fcap -c config.yaml backtest --trades trades.csv
$ start trades.csv
```

This replays the recorded funding with your settings. Read: `net pnl`, **`APR on
capital`**, `funding / costs`, number of trades. `start trades.csv` opens the trade
list in Excel (close it there before running the backtest again). Two weeks is short, so
treat it as a sanity check, not a verdict.

The backtest leaves out Coinbase's $0.20-per-contract minimum fee and the fact that
futures trade in whole contracts. It therefore looks better than reality on small
contracts (1000SHIB, 1000PEPE) and can show BTC trades the bot can't size at your
settings. When it and paper disagree, trust the paper numbers from `status`.

### After 4 weeks: tune and decide

```
$ fcap -c config.yaml sweep
```

This tries combinations of settings around yours. Each row is one combination:
`train_apr_pct` is its result on the first half of the data, `test_apr_pct` on the
second half, and `*` in the `yours` column marks your current settings. Good settings
have a good `test_apr_pct` close to `train_apr_pct`; don't just take the top row. Paste
the output to Claude if you want help choosing.

To change a setting: `notepad config.yaml`, edit, Ctrl + S, close Notepad. Keep the
indentation exactly as it was (spaces, never Tab). The paper engine reads settings when
it starts, so restart it: in a **new** Git Bash window type `bot` and
`touch state/paper/QUIT`, wait until the PAPER window shows the `$` prompt again, then
click the PAPER window, press **↑** (brings back the last command) and Enter.

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
   $ curl -s https://api64.ipify.org; echo
   ```

   If it prints four numbers with dots (like `203.0.113.7`), you can use it as the IP
   allowlist below. If it prints an address with colons, your connection uses IPv6,
   which Windows changes every day: leave the allowlist empty.

2. Go to **portal.cdp.coinbase.com** and sign in with your Coinbase account. Open
   **API keys** → **Secret API keys** → **Create API key**.
3. Settings:
   * Nickname: `funding-bot`
   * Portfolio: **Default**
   * Permissions: **View** ✅ **Trade** ✅ **Transfer** ❌ (leave off)
   * IP allowlist: the address from step 1. Leave it empty if step 1 printed colons or
     if your home IP changes from time to time: if it changes while a pair is open, the
     bot can't trade until you update the allowlist. The key can't move money either
     way.
   * Advanced settings → signature algorithm: **ECDSA**
4. Create. A file named like `cdp_api_key.json` downloads to your **Downloads**
   folder. Store it in the bot:

   ```
   $ bot
   $ ls ~/Downloads/*.json
   $ fcap keys coinbase --json ~/Downloads/cdp_api_key.json
   ```

   `ls` shows the exact file name. If your browser named it differently (for example
   `cdp_api_key (1).json`), put that name in quotes:
   `--json "$HOME/Downloads/cdp_api_key (1).json"`.

   You should see `saved COINBASE_API_KEY, COINBASE_SECRET_FILE to .env`. The private
   key itself goes to `C:\Users\<you>\.fcap\coinbase.pem`. **Only after you see that
   `saved …` line**, delete the download (Coinbase shows the key only once):

   ```
   $ rm ~/Downloads/cdp_api_key.json
   ```

   If your file had a different name, use that same quoted name, e.g.
   `rm "$HOME/Downloads/cdp_api_key (1).json"`. Then `ls ~/Downloads/*.json` should no
   longer list it.

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

For the first trial the bot opens **one** pair of at most $600. It buys the coin on
Kraken and holds the short on Coinbase (4b sets this up):

* **Kraken:** deposit about **$800 USD** (Funding → Deposit → USD → bank/ACH). It buys
  the spot coin with this.
* **Coinbase:** keep about **$800 USD** in the **Default** portfolio. Futures margin is
  drawn from that USD balance (Coinbase transfers it to the futures account itself; its
  futures help pages describe the timing). Overnight margin on alt-coin shorts can reach
  30–95% of their value, so keep the full amount there.

The bot doesn't check each exchange's cash separately, only `capital_usd`. A pair that
bought its coin on Coinbase would need about $1,200 there (the coin plus the margin),
which is why 4b keeps Coinbase to the short.

**Keep your own coins off this Kraken account** (at least the 24 coins in the Background
table): move them to another exchange or a wallet first. When something goes wrong the
bot can't tell its coins from yours, and it may sell yours to fix a pair.

### 4b. Switch the config to live, sized to one contract

```
$ notepad config.yaml
```

**Change these existing lines** (don't add a second `portfolio:` or `venues:` section;
the bot refuses duplicate sections):

```yaml
mode: live                      # was: paper
portfolio:
  capital_usd: 1500             # was: 5000 (a bit under the $1,600 deposited: fees)
  max_positions: 1              # was: 4
  max_position_usd: 600         # was: 1000
```

and **add one line** under `coinbase:` in the `venues:` section, with the same four
spaces in front as the lines around it:

```yaml
venues:
  coinbase:
    quotes: [USD, USDC]
    paper_leverage: 1.0
    kinds: [perp]               # add: Coinbase only for the short; coins come from Kraken
```

If this Kraken account is used only by the bot, also delete the `# ` (hash and space)
in front of `dead_man_seconds: 120` under `kraken:`. Kraken then cancels any order the
bot left open 2 minutes after the bot stops for any reason. Don't do it if you place
your own orders on that account: it would cancel those too.

Save and close Notepad, then check the file still loads:

```
$ fcap -c config.yaml check
```

(If it prints a long error mentioning `yaml`, see Troubleshooting.)

### 4c. Start it

First, stop Windows from restarting the PC by itself while real money is in play:
**Settings → Windows Update** (Windows 10: **Settings → Update & Security → Windows
Update**) → **Pause updates**, longest period. Windows then shows the date the pause
ends; it can't be paused for more than about 5 weeks. Also set **Advanced options →
Active hours** to cover as much of the day as it allows. Before the pause ends, update by
hand: stop the bot with QUIT (see "Stopping" below), install the updates and restart,
start LIVE again (here, 4c), then RECORD and PAPER (step 7), and pause again.

Open a new Git Bash window:

```
$ bot
$ printf '\e]0;LIVE\a'; fcap -c config.yaml --log-file logs/live.log live --i-understand-this-trades-real-money
```

Wait until you see `bootstrapped: …` and one `open positions: …` line, then minimise
the window; never close it while the bot runs (see "Stopping" below). It opens a pair
only when one passes your rules. That may take hours or days. The paper engine can keep
running alongside for comparison; it keeps separate records. If it says `another engine
is already running`, a LIVE window is already open: use that one.

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
`max_position_usd`. Set `capital_usd` to about 95% of what you deposited in total, and
keep each exchange at a bit more than half of `capital_usd`: Kraken pays for all the
coins plus their fees, Coinbase holds the margin for all the shorts. Add money to both
when you raise it. (If Kraken runs short of USD, the next buy is rejected and the bot
halts entries.)
Restart the live engine after each change: in a **new** Git Bash window `bot` and
`touch state/live/QUIT`, wait for the `$` prompt in the LIVE window, then click the LIVE
window, ↑, Enter.

### Stopping, pausing, emergencies

Run these in a **separate** Git Bash window after `bot`, **never in the LIVE window**:
while the bot runs there, that window ignores what you type (and may run it later, after
the bot has stopped).

| You want to | Do this |
|---|---|
| pause new trades (keep managing open ones) | `touch state/live/STOP` (resume: `rm state/live/STOP`) |
| close everything now | `touch state/live/FLATTEN` (trade again later: `rm state/live/FLATTEN`). If `logs/live.log` keeps showing `did not close (try …)` for a pair, `rm state/live/FLATTEN`, stop the bot with QUIT, close both legs of that pair by hand, then `fcap -c config.yaml forget --live <pair id>` (the id from `status --live`, like `P1760000000-1`) before starting it again |
| stop for good (close everything, then exit) | `touch state/live/FLATTEN` and `touch state/live/QUIT`. The bot closes every pair first (QUIT waits for that), then exits. When the LIVE window shows the `$` prompt, check both exchanges show no position, then `rm state/live/FLATTEN` |
| stop the program | `touch state/live/QUIT`, then wait until the LIVE window shows the `$` prompt. That is usually within a minute, but can take several minutes while it is opening or closing a pair, because it never stops halfway through an order. Only then close the window. Open pairs stay open and are managed again when you restart |
| restart the PC (e.g. to install updates) | `touch state/live/QUIT`, wait for the `$` prompt in the LIVE window, restart, then start LIVE again (4c), then RECORD and PAPER (step 7) |
| the PC restarted by itself (power cut, crash, update), or the LIVE window is gone | this is an unclean stop: do the checks below, then start LIVE again (4c), then RECORD and PAPER (step 7) |
| `status --live`, the LIVE window or `logs/live.log` shows `ENTRIES HALTED:` with any reason other than `STOP file present`, `FLATTEN file present` or `stale market data` | the bot hit something it won't guess about (an order it couldn't confirm, a leg left over, `close of … keeps failing`, or `untracked position on …`: a Coinbase position no pair accounts for). It keeps managing open pairs but opens none. Stop it with QUIT, do the checks below, fix by hand, restart. If the reason mentions a network error or timeout, first wait until `fcap -c config.yaml check` passes for both exchanges |
| `fcap … live` says `another engine is already running` | a LIVE window is still open (hover over the Git Bash icon in the taskbar). Use that one; never run two |

**Don't stop the live bot with Ctrl + C or the window's ✕.** In Git Bash both kill it
instantly, possibly in the middle of a trade. A power cut, a crash or Windows restarting
by itself does the same. **After any stop that didn't go through QUIT**, check before
starting it again:

1. On Kraken and on Coinbase, open the **Open orders** list and **Cancel** every order
   there. The bot never leaves an order open when it stops normally, so any open order
   is a leftover, and it could still fill.
2. **Coinbase → Futures → Positions:** each short must belong to a pair that
   `fcap -c config.yaml status --live` lists for that coin (item 4 covers one whose size
   differs). Close any short in a coin that status doesn't list. (If you miss one, the
   bot notices it and halts with `untracked position on …`.)
3. **Kraken (and Coinbase spot):** a coin the bot doesn't list, and that you didn't own
   before, is a leftover: sell it.
4. If `status --live` lists a pair whose coins or short are partly or fully gone, leave
   it: on restart the bot compares what is held with its record. If the legs differ a
   little it trims the larger one (note `rebalanced`); if both are smaller by the same
   small amount (up to a tenth) it shrinks its record (note `resized to holdings`); if
   more than a tenth of a leg is gone it closes the rest of the pair (log lines `legs
   badly mismatched … possible liquidation/ADL` and `FLATTENED`: expected after a stop in
   the middle of a close). If you also hold that coin yourself on that
   exchange, paste the details to Claude first.
5. If you closed both legs of a pair by hand, remove it from the bot's record before
   starting it again: `fcap -c config.yaml forget --live <pair id>`. Otherwise the bot
   sees the legs gone and may sell coins of that kind that are still on the account.

### Daily routine once live

```
$ bot
$ fcap -c config.yaml status --live
$ fcap -c config.yaml check                        # Coinbase "margin buffer" line
$ grep -iE "critical|error|halted|still open" logs/live.log | tail
```

* The second line of `status --live` must say `engine: running, last saved …`. If it
  says `WARNING: the live engine is NOT running`, nothing is watching your open pairs:
  do the checks under "Stopping" and start it again (4c). If it says `NOTE: it is
  running but has not saved for a while`, leave the exchanges alone: the bot is busy
  with an order or can't write its file (look for `cannot write` in the LIVE window).
* If it shows `ENTRIES HALTED:`, see the table under "Stopping".
* Margin buffer: the bot warns at 35% and closes pairs at 20%. Add USD to Coinbase well
  before that.
* Weekly: compare the bot's positions and funding with both exchanges' statements.

---

## Background: what you can trade from the US

| Venue | What the bot uses it for | Status for a US person |
|---|---|---|
| **Coinbase** (Advanced) | spot long legs **and** the short perp leg: CFTC-regulated perpetual-style futures on 24 coins (BTC, ETH, SOL, XRP, DOGE, ADA, LINK, AVAX, LTC, DOT, XLM, HBAR, SUI, BCH, BNB, NEAR, AAVE, ONDO, ENA, HYPE, ZEC, PAXG, 1000SHIB, 1000PEPE as of October 2026), hourly funding | ✅ after futures onboarding |
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
| `bash: bot: command not found` | see "If a new window says `bash: bot: command not found`" in step 3 |
| `bash: py: command not found` | Python isn't installed, or Git Bash was opened before installing it: close and reopen Git Bash; if it persists, install from the 3.12.10 link in step 1 with **Add python.exe to PATH** ticked |
| `python` opens the Microsoft Store | use `py -3.12` (needed only before step 3); or turn off **Settings → Apps → Advanced app settings → App execution aliases → python.exe / python3.exe** |
| `.venv/bin/activate: No such file or directory` | on Windows it's `.venv/Scripts/activate` |
| pasting does nothing, or the line starts with `^[[200~` | press Backspace until the line is empty, then **Shift + Insert** or right-click → Paste (not Ctrl + V) |
| a Git Bash window stopped showing anything after you pressed Ctrl + S in it | click in it and press **Ctrl + Q** |
| `git clone` asks for a username/password in the window | the sign-in window didn't appear: reinstall Git for Windows with the default options (they include Git Credential Manager) and clone again |
| `FAIL markets: NetworkError` | internet, VPN or firewall, or the exchange is down: retry |
| `FAIL credentials: COINBASE_SECRET_FILE: cannot read …` | re-run `fcap keys coinbase --json <file>` with a freshly created key |
| `FAIL balance: AuthenticationError` | wrong key or missing permission. If you set an IP allowlist, empty it on the exchange and run `check` again (your IP changed, or your connection uses IPv6) |
| Kraken `EAPI:Invalid nonce`, or Coinbase `401` | clock: **Settings → Time & language → Date & time → Sync now**. If it persists, the key was used by another program: make a new key just for the bot |
| Coinbase order `403` / "onboarded users" | finish futures onboarding (3a) |
| `cannot write …engine.json` (CRITICAL) | another program (Excel, an editor, a sync or backup tool) has the bot's state file open: close it. Until the file can be written the bot opens no new pairs, and a QUIT waits (`QUIT waits: … cannot be written`): the bot exits by itself as soon as it can write the file again |
| `PermissionError … trades.csv` | the file is still open in Excel: close it there, then run the backtest again |
| a long error mentioning `yaml` (`ScannerError`, `ParserError`, `expected <block end>`, `found character '\t'`, `mapping values are not allowed here`, `could not find expected ':'`), or `expected settings indented below it`, right after editing config.yaml | `notepad config.yaml` and look at the lines you changed: they must start with exactly as many spaces as the lines they belong with (never Tab), and have a space after each colon (`capital_usd: 1500`). Save, close, try again |
| `another engine is already running on state/…` | that engine's window is still open (hover over the Git Bash icon in the taskbar): use it. Never run two |
| `status` prints `WARNING: the … engine is NOT running` | start it again (paper: step 7; live: the checks under "Stopping", then 4c) |
| `status` prints `NOTE: it is running but has not saved for a while` | it is busy with an order, or can't write its file: see the `cannot write` row. Don't touch the exchanges or start a second copy |
| `duplicate key '…'` (e.g. `portfolio`, `venues`) | you pasted a second copy of a section into config.yaml: merge them into one |
| `fees.kraken.spot_maker … fractions` | write `0.004`, not `0.4` or `0.4%` |
| `found state/engine.json from an older version` | move it to `state/paper/` or `state/live/` as the message says |
| `scan` never shows `*` | normal in quiet markets; check your `fees:` are right |
| many `maker order … timed out` in the log | spot orders aren't filling: raise `execution.maker_timeout_s` a little, or that coin is too thin |
| a window shows nothing new for a long time | run `fcap -c config.yaml status` (add `--live` for the live bot). `NOT running`: start it again. `engine: running` with a recent save: the window is just paused; click in it and press **Ctrl + Q** |

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
fcap -c config.yaml forget [--live] PAIR_ID     drop a pair you closed by hand (bot stopped)
global options go right after `fcap`, before the command name:
  -c FILE   -v (debug)   --log-file FILE   --env-file FILE (default .env)
  right: fcap -c config.yaml --log-file logs/paper.log paper
  wrong: fcap -c config.yaml paper --log-file logs/paper.log   (unrecognized arguments)

kill switches (paper engine: state/paper/, live engine: state/live/)
touch state/live/STOP       no new pairs            rm state/live/STOP to resume
touch state/live/FLATTEN    close all pairs         rm state/live/FLATTEN to trade again
touch state/live/QUIT       exit cleanly between steps (the file removes itself)
run them in a separate Git Bash window, never in the window the bot runs in
```

## On a Mac or Linux instead

The same steps work in Terminal with these changes: install Python 3.12 with your
package manager and type `python3.12` where this says `py -3.12`; activate with
`source .venv/bin/activate` (also in the `bot` line, which goes into `~/.zshrc` on a
Mac); run the long-lived programs under `tmux` (or `nohup … &`) instead of in separate
windows; keep a Mac awake with `caffeinate -dims`. Stop the bot with QUIT there too
(Ctrl + C can also interrupt it mid-trade). `fcap keys` hides the private key while you
paste it.
