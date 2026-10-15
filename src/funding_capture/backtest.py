"""Event-driven backtest of funding capture on historical settlement data.

What is modelled
    * every settlement on every perp leg, paid on fixed USD notional
    * round-trip fees + slippage from ``CostModel`` (entry at open, exit at close)
    * borrow cost on short-spot legs
    * decisions use only settlements already printed (no look-ahead, and no access to
      the live "predicted" rate, so it is somewhat pessimistic about entry timing)

What is NOT modelled (and will make live results worse than the backtest)
    * basis/mark divergence between legs at entry and exit
    * liquidations, ADL, margin top-ups, and transfers between venues
    * market impact beyond ``slippage_bps``; capacity; spot listing dates
    * survivorship: data is downloaded for markets that exist *today*
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from .costs import CostModel
from .forecast import ewma_frame, horizon_average
from .models import HOURS_PER_YEAR, MS_PER_HOUR, Candidate, Instrument, Kind
from .opportunities import StrategyParams, build_candidates
from .rates import DEFAULT_INTERVAL_HOURS, infer_interval_hours

_STANDARD_INTERVALS = np.array([1.0, 2.0, 4.0, 8.0])


@dataclass(frozen=True)
class BacktestConfig:
    capital_usd: float = 10_000.0
    max_positions: int = 5
    max_positions_per_asset: int = 1
    min_position_usd: float = 100.0
    warmup_hours: int = 72
    # Ignore entries where a leg prints above this |APR|; such prints are usually squeezes
    # in thin markets that a backtest cannot trade realistically. None disables.
    max_leg_abs_apr: float | None = 5.0
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass
class _Open:
    cand_idx: int
    notional: float
    capital: float
    opened_t: int
    funding: float = 0.0
    borrow: float = 0.0
    costs: float = 0.0


@dataclass
class BacktestResult:
    summary: dict
    equity: pd.Series  # USD, hourly
    trades: pd.DataFrame
    params: StrategyParams = field(repr=False, default=None)

    def report(self) -> str:
        s = self.summary
        lines = [
            f"period          {s['start']} -> {s['end']} ({s['days']:.0f} days)",
            f"capital         ${s['capital_usd']:,.0f}",
            f"net pnl         ${s['net_pnl_usd']:,.2f}  ({s['return_pct']:.2f}%)",
            f"APR on capital  {s['apr_pct']:.2f}%",
            f"funding / costs / borrow  ${s['funding_usd']:,.2f} / ${s['costs_usd']:,.2f}"
            f" / ${s['borrow_usd']:,.2f}",
            f"trades          {s['trades']}  (win rate {s['win_rate_pct']:.0f}%,"
            f" avg hold {s['avg_hold_hours']:.0f}h)",
            f"utilisation     {s['avg_utilisation_pct']:.0f}% of capital deployed on average",
            f"max drawdown    ${s['max_drawdown_usd']:,.2f}",
            f"sharpe (daily)  {s['sharpe']:.2f}",
        ]
        if not self.trades.empty:
            by = self.trades.groupby("construction")["net_usd"].agg(["count", "sum"])
            lines.append("by construction:")
            for k, row in by.iterrows():
                lines.append(f"  {k:<18} {int(row['count']):>5} trades  ${row['sum']:>12,.2f}")
            top = self.trades.groupby("asset")["net_usd"].sum().sort_values(ascending=False)
            lines.append(
                "top assets: " + ", ".join(f"{a} ${v:,.0f}" for a, v in top.head(8).items())
            )
        return "\n".join(lines)


def _settlements(df: pd.DataFrame, venue: str) -> pd.DataFrame:
    """Per-settlement rows with hour index, interval and hourly-normalised rate."""
    df = df.sort_values("ts_ms").drop_duplicates("ts_ms")
    ts = df["ts_ms"].to_numpy(dtype=np.int64)
    rate = df["rate"].to_numpy(dtype=float)
    default = infer_interval_hours(ts.tolist()) or DEFAULT_INTERVAL_HOURS.get(venue, 8.0)
    gaps = np.diff(ts, prepend=ts[0] - int(default * MS_PER_HOUR)) / MS_PER_HOUR
    if len(gaps) > 1:
        gaps[0] = gaps[1]  # the first row's interval is unknown; assume it matches the next
    # regular gaps snap to the nearest standard interval; data holes keep the last one
    snapped = _STANDARD_INTERVALS[np.abs(gaps[:, None] - _STANDARD_INTERVALS).argmin(axis=1)]
    regular = (gaps >= 0.5) & (gaps <= 8.5)
    interval = pd.Series(np.where(regular, snapped, np.nan)).ffill().fillna(default).to_numpy()
    return pd.DataFrame(
        {
            "hour": ts // MS_PER_HOUR,
            "rate": rate,
            "interval": interval,
            "hourly": rate / interval,
        }
    )


class Backtester:
    def __init__(
        self,
        instruments: list[Instrument],
        series: dict[str, pd.DataFrame],
        params: StrategyParams,
        costs: CostModel,
        config: BacktestConfig | None = None,
    ):
        self.params = params
        self.costs = costs
        self.cfg = config or BacktestConfig()
        perps = [i for i in instruments if i.kind is Kind.PERP and i.key in series]
        perp_assets = {p.asset for p in perps}
        spots = [i for i in instruments if i.kind is Kind.SPOT and i.asset in perp_assets]
        self.instruments = perps + spots
        self._prepare(perps, series)

    # ------------------------------------------------------------------ preparation
    def _prepare(self, perps: list[Instrument], series: dict[str, pd.DataFrame]) -> None:
        sett = {p.key: _settlements(series[p.key], p.venue) for p in perps}
        lo = min(int(s["hour"].min()) for s in sett.values())
        hi = max(int(s["hour"].max()) for s in sett.values())
        if self.cfg.start_ms is not None:
            lo = max(lo, self.cfg.start_ms // MS_PER_HOUR)
        if self.cfg.end_ms is not None:
            hi = min(hi, self.cfg.end_ms // MS_PER_HOUR)
        if hi <= lo:
            raise ValueError("no overlapping data in the requested window")
        hours = np.arange(lo, hi + 1)
        self.hours = hours
        n_t, n_p = len(hours), len(perps)
        known = np.full((n_t, n_p), np.nan)
        pay = np.zeros((n_t, n_p))
        for j, p in enumerate(perps):
            s = sett[p.key]
            s = s[(s["hour"] >= lo) & (s["hour"] <= hi)]
            if s.empty:
                continue
            idx = (s["hour"].to_numpy() - lo).astype(int)
            np.add.at(pay[:, j], idx, s["rate"].to_numpy())
            col = pd.Series(np.nan, index=range(n_t))
            col.iloc[idx] = s["hourly"].to_numpy()
            limit = int(2 * s["interval"].max()) + 1  # stale after two missed settlements
            known[:, j] = col.ffill(limit=limit).to_numpy()
        self.perps = perps
        self.known = known
        self.pay = pay
        self.col_of = {p.key: j for j, p in enumerate(perps)}

    def _forecast(self, params: StrategyParams) -> np.ndarray:
        known = pd.DataFrame(self.known)
        level = ewma_frame(known, params.forecast.halflife_hours).to_numpy()
        anchor = None
        if params.forecast.anchor_halflife_hours:
            anchor = ewma_frame(known, params.forecast.anchor_halflife_hours).to_numpy()
        fc = horizon_average(level, params.forecast, params.horizon_hours, anchor)
        return np.hstack([fc, np.zeros((fc.shape[0], 1))])  # last column: spot legs (0)

    def _candidate_arrays(self, cands: list[Candidate]) -> dict[str, np.ndarray]:
        zero = len(self.perps)

        def col(i: Instrument) -> int:
            return self.col_of[i.key] if i.kind is Kind.PERP else zero

        return {
            "long": np.array([col(c.long) for c in cands], dtype=int),
            "short": np.array([col(c.short) for c in cands], dtype=int),
            "borrow": np.array([self.costs.borrow_hourly(c) for c in cands]),
            "rtc": np.array([self.costs.round_trip_cost(c) for c in cands]),
            "cap": np.array([self.costs.capital_per_notional(c) for c in cands]),
        }

    # ------------------------------------------------------------------ simulation
    def run(self, params: StrategyParams | None = None) -> BacktestResult:
        params = params or self.params
        cfg = self.cfg
        cands = build_candidates(self.instruments, params)
        if not cands:
            raise ValueError("no candidates: check constructions / instruments")
        arr = self._candidate_arrays(cands)
        fc = self._forecast(params)
        known = np.hstack([self.known, np.zeros((len(self.hours), 1))])
        pay = np.hstack([self.pay, np.zeros((len(self.hours), 1))])
        H = params.horizon_hours
        cand_asset = np.array([c.asset for c in cands])
        cand_insts = [(c.long.key, c.short.key) for c in cands]

        cash = cfg.capital_usd
        open_pos: dict[int, _Open] = {}
        trades: list[dict] = []
        equity = np.empty(len(self.hours))
        deployed = np.zeros(len(self.hours))
        next_id = itertools.count(1)

        def close(pos_id: int, t: int, reason: str) -> None:
            nonlocal cash
            pos = open_pos.pop(pos_id)
            exit_cost = pos.notional * arr["rtc"][pos.cand_idx] / 2
            pos.costs += exit_cost
            cash -= exit_cost
            c = cands[pos.cand_idx]
            trades.append(
                {
                    "id": pos_id,
                    "construction": str(c.construction),
                    "asset": c.asset,
                    "long": f"{c.long.venue}:{c.long.kind}",
                    "short": f"{c.short.venue}:{c.short.kind}",
                    "opened": _iso(self.hours[pos.opened_t]),
                    "closed": _iso(self.hours[t]),
                    "hours": int(t - pos.opened_t),
                    "notional_usd": pos.notional,
                    "capital_usd": pos.capital,
                    "funding_usd": pos.funding,
                    "borrow_usd": pos.borrow,
                    "costs_usd": pos.costs,
                    "net_usd": pos.funding - pos.borrow - pos.costs,
                    "exit_reason": reason,
                }
            )

        for t in range(len(self.hours)):
            # 1. settlements and borrow accrue on positions held into this hour
            for pos in open_pos.values():
                li, si = arr["long"][pos.cand_idx], arr["short"][pos.cand_idx]
                f = (pay[t, si] - pay[t, li]) * pos.notional
                b = arr["borrow"][pos.cand_idx] * pos.notional
                pos.funding += f
                pos.borrow += b
                cash += f - b

            carry = fc[t, arr["short"]] - fc[t, arr["long"]] - arr["borrow"]
            fcst_apr_cap = carry * HOURS_PER_YEAR / arr["cap"]

            # 2. exits
            for pos_id in list(open_pos):
                pos = open_pos[pos_id]
                ci = pos.cand_idx
                if np.isnan(carry[ci]):
                    close(pos_id, t, "no data")
                elif fcst_apr_cap[ci] < params.exit_apr:
                    close(pos_id, t, "carry below exit")
                elif (
                    params.max_hold_hours is not None and t - pos.opened_t >= params.max_hold_hours
                ):
                    close(pos_id, t, "max hold")

            # 3. entries
            if t >= cfg.warmup_hours and len(open_pos) < cfg.max_positions:
                net_apr = (carry * H - arr["rtc"]) / H * HOURS_PER_YEAR / arr["cap"]
                with np.errstate(divide="ignore", invalid="ignore"):
                    be = np.where(carry > 0, arr["rtc"] / carry, np.inf)
                ok = (net_apr >= params.entry_apr) & (be <= params.max_breakeven_hours)
                if cfg.max_leg_abs_apr is not None:
                    lim = cfg.max_leg_abs_apr / HOURS_PER_YEAR
                    ok &= (np.abs(known[t, arr["long"]]) <= lim) & (
                        np.abs(known[t, arr["short"]]) <= lim
                    )
                ok &= ~np.isnan(net_apr)
                if ok.any():
                    held_assets: dict[str, int] = {}
                    used_insts: set[str] = set()
                    for pos in open_pos.values():
                        a = cands[pos.cand_idx].asset
                        held_assets[a] = held_assets.get(a, 0) + 1
                        used_insts.update(cand_insts[pos.cand_idx])
                    used_capital = sum(p.capital for p in open_pos.values())
                    for ci in np.flatnonzero(ok)[np.argsort(-net_apr[ok])]:
                        if len(open_pos) >= cfg.max_positions:
                            break
                        a = cand_asset[ci]
                        if held_assets.get(a, 0) >= cfg.max_positions_per_asset:
                            continue
                        if used_insts.intersection(cand_insts[ci]):
                            continue  # an instrument belongs to at most one open pair
                        budget = min(
                            cfg.capital_usd / cfg.max_positions, cfg.capital_usd - used_capital
                        )
                        notional = budget / arr["cap"][ci]
                        if notional < cfg.min_position_usd:
                            break
                        entry_cost = notional * arr["rtc"][ci] / 2
                        cash -= entry_cost
                        open_pos[next(next_id)] = _Open(ci, notional, budget, t, costs=entry_cost)
                        held_assets[a] = held_assets.get(a, 0) + 1
                        used_insts.update(cand_insts[ci])
                        used_capital += budget

            # mark-to-market: open positions are worth cash minus their (unpaid) exit cost
            pending_exit = sum(p.notional * arr["rtc"][p.cand_idx] / 2 for p in open_pos.values())
            equity[t] = cash - pending_exit
            deployed[t] = sum(p.capital for p in open_pos.values())

        last = len(self.hours) - 1
        for pos_id in list(open_pos):
            close(pos_id, last, "end of data")
        equity[last] = cash

        eq = pd.Series(equity, index=pd.to_datetime(self.hours * MS_PER_HOUR, unit="ms", utc=True))
        tr = pd.DataFrame(trades)
        return BacktestResult(_summary(eq, tr, deployed, cfg), eq, tr, params)


def _iso(hour: int) -> str:
    return pd.Timestamp(int(hour) * MS_PER_HOUR, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")


def _summary(eq: pd.Series, tr: pd.DataFrame, deployed: np.ndarray, cfg: BacktestConfig) -> dict:
    cap = cfg.capital_usd
    days = max((eq.index[-1] - eq.index[0]).total_seconds() / 86400, 1e-9)
    pnl = float(eq.iloc[-1] - cap)
    daily = eq.resample("1D").last().diff().dropna() / cap
    sharpe = float(daily.mean() / daily.std() * math.sqrt(365)) if daily.std() > 0 else 0.0
    dd = float((eq.cummax() - eq).max())
    has = not tr.empty
    return {
        "start": str(eq.index[0].date()),
        "end": str(eq.index[-1].date()),
        "days": days,
        "capital_usd": cap,
        "net_pnl_usd": pnl,
        "return_pct": pnl / cap * 100,
        "apr_pct": pnl / cap * 365 / days * 100,
        "funding_usd": float(tr["funding_usd"].sum()) if has else 0.0,
        "costs_usd": float(tr["costs_usd"].sum()) if has else 0.0,
        "borrow_usd": float(tr["borrow_usd"].sum()) if has else 0.0,
        "trades": len(tr),
        "win_rate_pct": float((tr["net_usd"] > 0).mean() * 100) if has else 0.0,
        "avg_hold_hours": float(tr["hours"].mean()) if has else 0.0,
        "avg_utilisation_pct": float(deployed.mean() / cap * 100),
        "max_drawdown_usd": dd,
        "sharpe": sharpe,
    }


def sweep(
    bt: Backtester,
    grid: dict[str, list],
    split: float = 0.5,
) -> pd.DataFrame:
    """Grid search with a walk-forward split: pick on the first part, judge on the second.

    ``grid`` keys are StrategyParams fields, or ``forecast.<field>`` for ForecastParams.
    Ranking by in-sample APR alone overfits; read the ``test_apr_pct`` column.
    """
    hours = bt.hours
    cut_ms = int(hours[0] + (hours[-1] - hours[0]) * split) * MS_PER_HOUR
    train = _window(bt, end_ms=cut_ms)
    test = _window(bt, start_ms=cut_ms)
    keys = list(grid)
    rows = []
    for values in itertools.product(*(grid[k] for k in keys)):
        p = bt.params
        for k, v in zip(keys, values, strict=True):
            if k.startswith("forecast."):
                p = replace(p, forecast=replace(p.forecast, **{k.split(".", 1)[1]: v}))
            else:
                p = replace(p, **{k: v})
        a, b = train.run(p).summary, test.run(p).summary
        rows.append(
            {
                **dict(zip(keys, values, strict=True)),
                "train_apr_pct": a["apr_pct"],
                "train_trades": a["trades"],
                "test_apr_pct": b["apr_pct"],
                "test_trades": b["trades"],
                "test_max_dd_usd": b["max_drawdown_usd"],
            }
        )
    return pd.DataFrame(rows).sort_values("train_apr_pct", ascending=False)


def _window(bt: Backtester, start_ms: int | None = None, end_ms: int | None = None) -> Backtester:
    """A view of ``bt`` restricted to a time window, sharing the prepared matrices."""
    lo = 0 if start_ms is None else int(np.searchsorted(bt.hours, start_ms // MS_PER_HOUR))
    hi = len(bt.hours) if end_ms is None else int(np.searchsorted(bt.hours, end_ms // MS_PER_HOUR))
    view = object.__new__(Backtester)
    view.__dict__.update(bt.__dict__)
    view.hours = bt.hours[lo:hi]
    view.known = bt.known[lo:hi]
    view.pay = bt.pay[lo:hi]
    return view
