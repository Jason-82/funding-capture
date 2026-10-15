# Funding capture: the search space

This note maps out the ways to harvest perpetual-futures funding without taking a view on
price, what each one costs and risks, and how the code in this repo tests them. Numbers
in the worked examples are arithmetic on stated assumptions, not observed market data.

## TL;DR

* Funding is a payment between longs and shorts on a perp, usually every 8h (hourly on
  Hyperliquid/dYdX). Positive funding: longs pay shorts. Holding the *receiving* side of
  the perp and hedging the price exposure elsewhere turns funding into a yield.
* There are three hedged constructions worth building: **spot long + perp short**,
  **perp long + perp short across venues**, and **spot short (borrowed) + perp long**.
  Everything else is a variation on capital efficiency, timing or execution.
* Whether it is profitable is decided by four things, roughly in this order:
  1. **Persistence vs. costs.** A rate only pays if it lasts long enough to cover four
     fills (two legs × in and out). The key number is *breakeven hours* = round-trip
     cost ÷ hourly carry.
  2. **Capital efficiency.** Each dollar of hedged notional ties up 1.0 to 1.5+ dollars of
     capital depending on construction, leverage and whether margin is unified.
  3. **Tail losses.** One leg liquidated, auto-deleveraged, or simply not the same
     asset as the other. One bad event can erase months of carry.
  4. **Fee tier.** Base-tier taker fees make many opportunities not worth taking. Maker
     execution or a VIP tier changes the answer.
* **Correction on the negative-funding idea.** When funding is negative, longs get paid,
  so you go long the perp. But pairing that with a *spot long* on another exchange gives
  you two long positions: twice the price exposure and no hedge. The hedge for a long
  perp has to be a *short*: borrow and sell the spot (construction C), or short the same
  coin's perp on a venue where funding is higher (construction B). B is usually the
  practical one, because negative funding means many people want to be short, which is
  exactly when spot borrow dries up or gets expensive.

## 1. Mechanics that matter

**Formula.** Binance-style venues (most others use a variant) compute
`funding = premium + clamp(interest − premium, ±0.05%)` per 8h, where `interest` is fixed
at 0.01%/8h. When the perp trades close to spot, funding sits at the **baseline of
0.01%/8h = 10.95% APR**, positive. This baseline is the floor of the classic
cash-and-carry: in calm, mildly bullish markets you earn about 11% APR on notional before
costs, and anything above that comes from demand for leveraged longs.

**Intervals differ.** Binance/Bybit/OKX default to 8h, many alts settle every 4h or 1h,
and venues shorten intervals when a market pins its cap. Hyperliquid and dYdX settle
hourly. All comparisons must be per hour (`rate / interval_hours`). The code normalises
everywhere and detects interval changes in historical data.

**Settlement is a snapshot on CEXs.** Whoever holds the position at the settlement
timestamp pays or receives the whole interval's funding: there is no pro-rata. That has
two consequences:
* don't open a pair seconds before a settlement where you would *pay*;
* don't close one seconds before a settlement where you would *receive*.
The engine's `settlement_guard_minutes` does both.

Trying to farm the snapshot itself (in just before, out just after) mostly fails: the
perp's premium tends to compress into the settlement, and four fills of fees exceed one
interval of funding unless the rate is extreme.

**Predicted rate.** Venues publish the in-progress rate for the next settlement. The
engine blends it with history (`live_weight`). The backtest cannot see it, because
historical data only has settled rates, which makes the backtest slightly pessimistic
about entry timing.

**Caps.** Each market has a max/min funding per interval. When one venue hits its cap and
another does not, the spread can persist for a long time: a source of B opportunities.

## 2. The constructions

| | Construction | Earns | Capital per $1 notional | Main extra cost | Main risks |
|---|---|---|---|---|---|
| A | Long spot + short perp (`spot_perp`) | positive funding on the perp | 1 + 1/L (≈1.0 with unified margin) | spot fees (often 2× perp fees) | short perp liquidated in a rally if the spot sits on another venue |
| B | Long perp (venue X) + short perp (venue Y) (`perp_perp`) | funding spread f_Y − f_X, either sign | 2/L | 4 perp fills | one leg liquidated or ADL'd; the two marks diverge |
| C | Short spot (borrowed) + long perp (`reverse_spot_perp`) | negative funding minus borrow cost | spot margin + 1/L | borrow interest (variable, can spike) | borrow recalled or unavailable; short squeeze on the spot margin |
| D | Long spot + short dated future (not built) | fixed basis to expiry | 1 + 1/L | roll cost | locks a rate: good benchmark against A |
| ✗ | Long perp + long spot | nothing hedged | – | – | 2× directional exposure: **not a carry trade** |

L = leverage on each perp leg (notional / margin posted).

### Worked example A: BTC at baseline funding

* Funding 0.01%/8h = 0.00125%/h = 10.95% APR on notional.
* Base-tier taker fees: spot 0.10% and perp 0.05% per fill → round trip
  2 × (0.10% + 0.05%) = **0.30%**.
* Breakeven: 0.30% ÷ 0.00125%/h = **240 hours (10 days)**.
* Held a full year: 10.95% − 0.30% = 10.65% on notional. On capital:
  * separate wallets, 2× short: 1.5 capital per notional → **7.1%**
  * 3× short: 1.33 → 8.0%
  * unified/portfolio margin, spot counted as collateral: ≈1.0 → ≈10.6%

So baseline BTC carry is a T-bill-like yield with exchange risk on top. It only gets
interesting in elevated-funding regimes, with a better fee tier, or with unified margin.

### Worked example B: alt perp/perp spread

* Venue X at 0.05%/8h (54.75% APR), venue Y at 0.01%/8h. Long Y, short X: spread
  0.04%/8h = 0.005%/h = **43.8% APR on notional**.
* Four perp taker fills at 0.05% = 0.20% → breakeven **40 hours**.
* 2× per leg → capital 1.0 per notional → 43.8% gross on capital *while it lasts*.
* If the spread holds for a week: 168h × 0.005% = 0.84% − 0.20% = **0.64% net per week**.
* If it decays with a 72h half-life instead, the average over the week is about half
  (factor 0.498): 0.42% − 0.20% = **0.22% per week**. How fast spreads decay is
  the single most important unknown, and it is what the forecast parameters and the
  sweep are for.

### Worked example C: negative funding with borrow

* Funding −0.10%/8h (−109.5% APR, longs paid), borrow at 50% APR → 59.5% APR on
  notional on paper.
* In practice the borrow pool is often empty for coins with deeply negative funding, the
  borrow rate is variable, and negative funding frequently precedes short squeezes, which
  hit the spot-short leg's margin. Treat C as opportunistic: off by default and only
  enabled for venues listed in `borrow_venues`. The bot does not place margin-borrow
  orders yet (see gaps).

## 3. Dimensions of the search space

These are the knobs, and each maps to config so the backtest/sweep can test it:

1. **Venue set** (`venues`). CEX perps (Binance, Bybit, OKX, Bitget, Gate…) and on-chain
   perps (Hyperliquid, dYdX…). *Hypothesis to test*: CEX↔DEX spreads are wider and more
   persistent than CEX↔CEX because of different user bases, formulas, caps and intervals,
   and less arbitrage capital. The data will say.
2. **Universe** (`min_volume_usd`, `include_assets`, `max_leg_abs_apr`). Majors have low
   funding, deep books and huge capacity. Mid-caps have higher funding and adequate
   liquidity, and are probably where the edge is. The long tail and new listings show
   extreme prints with thin books and delisting risk, and are where backtests lie most.
3. **Construction mix** (`constructions`, `cross_venue_spot`, `borrow_venues`).
4. **Signal** (`forecast.*`):
   * `halflife_hours`: smoothing of observed funding;
   * `reversion_halflife_hours`: how fast elevated funding is assumed to fade;
   * `anchor_apr` vs `anchor_halflife_hours`: fade toward the global ~11% baseline (default,
     conservative) or toward each market's own 30-day level (lets structurally persistent
     venue spreads count);
   * `live_weight`: trust in the venue's predicted next rate.
5. **Entry/exit** (`entry_apr`, `exit_apr`, `horizon_hours`, `max_breakeven_hours`,
   `max_hold_hours`). Entry requires a forecast net APR on capital *after* round-trip
   costs. Exit has hysteresis: entry costs are sunk, so a position is held until its
   forecast carry falls below `exit_apr`.
6. **Capital efficiency** (`perp_leverage`, `unified_margin_credit`). Higher leverage
   means more APR on capital and closer liquidation. 2× gives a ~50% adverse move to
   liquidation on a perp leg (minus maintenance margin); 3× gives ~33%.
7. **Execution** (`execution.*`, `costs.taker`, `slippage_bps`). Working the spot leg
   passively would roughly halve A's cost. Not implemented yet.
8. **Sizing and concentration** (`max_positions`, `max_position_usd`,
   `max_positions_per_asset`, `max_venue_notional_usd`).

## 4. Risks and what the bot does about each

| Risk | What happens | In the code | Still open |
|---|---|---|---|
| Liquidation of a perp leg | Price runs against the leg whose collateral is separate from the hedge's profit | 2× default leverage; liq-distance check warns at 35%, closes the pair at 20% | No automatic margin transfer between venues |
| ADL / forced close | Venue closes your profitable leg in a crash; you are left naked | Every loop reconciles venue positions against expected; mismatch over 10% → flatten each leg independently + cooldown | Detection is per loop (default 60s) |
| Funding flips | Carry turns into a cost | Forecast-based exit with hysteresis | Forecast quality |
| Leg execution risk | One leg fills, the other doesn't | Thinner leg first, IOC slices, immediate hedge with widening limits, unwind on failure, residual → halts entries | Gaps faster than IOC retries |
| Not the same asset | Ticker collision across venues, wrapped/bridged token, stablecoin depeg | Multiplier/alias normalisation; leg prices must agree within 1% to enter; >3% divergence closes | USDT vs USDC treated as 1:1 |
| Basis at exit | Perp premium at entry/exit differs from spot | Partly in `slippage_bps` | Not modelled in backtest; basis-aware entry is a TODO |
| Stale/missing data | Bot acts on old prices | Data older than `max_data_age_s` halts entries; positions on that venue are not judged blind | – |
| Venue failure | Insolvency, withdrawal freeze, outage | Per-venue notional cap; `STOP`/`FLATTEN` kill files | Cannot be hedged: size accordingly |
| Delisting | Perp delisted at short notice | Missing data forces an exit | Backtest sees only markets alive today (survivorship) |
| Operational | Key leaks, manual trades on bot instruments | Keys only from env; bot assumes it owns its instruments | Use sub-accounts, IP allowlists, no withdrawal permission |
| Legal/tax | Venue access by jurisdiction; funding is taxable income in many places | – | Your call: check before using any venue |

## 5. How we find out whether it makes money

1. `fcap fetch --days 365` on 3–4 venues, top ~150 perps by volume each.
2. `fcap backtest`: read the split of funding vs. costs, trades per construction, average
   hold, and utilisation (how much of the capital was actually working).
3. `fcap sweep`: walk-forward. Pick parameters on the first half, judge on the second.
   Choose a region of parameters that holds up out of sample, not the single best row.
4. Stress it: set your real fee tier, double `slippage_bps`, raise `min_volume_usd` to
   20M. If the result collapses, the edge was in markets you can't actually trade.
5. `fcap paper` for 2–4 weeks: compare each paper trade's realised funding with what the
   backtest would have predicted for the same window.
6. Live with small size on one construction, then scale.

**Kill criterion.** If backtested net APR on capital does not clearly beat a stablecoin
savings rate plus a premium for exchange risk (say 10%+ APR above it), it isn't worth the
counterparty risk.

## 6. Known gaps (next steps, roughly by value)

1. Automated margin top-ups/transfers between venues for cross-venue pairs.
2. Maker (post-only) execution for the spot leg.
3. Borrow availability and rate feed, plus margin-borrow orders, for construction C.
4. Basis-aware entry: prefer entering A when the perp trades at a premium.
5. Rotation: swap a weak open pair for a much better one when slots are full.
6. Delisted-market history and spot listing dates in the backtest.
7. Alerts (Telegram/Slack/email) on CRITICAL risk events.
