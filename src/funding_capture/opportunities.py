"""Enumerate delta-neutral pairings and score them net of costs.

Shared by the scanner, backtester and engine so all three agree on what an
"opportunity" is worth.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from .costs import CostModel
from .forecast import ForecastParams
from .models import HOURS_PER_YEAR, Candidate, Construction, Instrument, Kind
from .rates import breakeven_hours, candidate_carry_hourly

QUOTE_PREFERENCE = ("USDT", "USDC", "USD", "FDUSD")


@dataclass(frozen=True)
class StrategyParams:
    constructions: tuple[Construction, ...] = (Construction.SPOT_PERP, Construction.PERP_PERP)
    cross_venue_spot: bool = True  # allow spot on venue A hedging a perp on venue B
    borrow_venues: tuple[str, ...] = ()  # venues where we can short spot via margin borrow
    horizon_hours: float = 168.0  # hold horizon the entry decision is evaluated over
    entry_apr: float = 0.15  # min forecast net APR on capital (after round-trip costs)
    exit_apr: float = 0.0  # close when forecast carry APR on capital falls below this
    max_breakeven_hours: float = 72.0  # skip trades that take longer than this to pay fees
    max_hold_hours: float | None = None
    forecast: ForecastParams = field(default_factory=ForecastParams)


def _quote_rank(q: str) -> int:
    return QUOTE_PREFERENCE.index(q) if q in QUOTE_PREFERENCE else len(QUOTE_PREFERENCE)


def build_candidates(instruments: Iterable[Instrument], params: StrategyParams) -> list[Candidate]:
    perps: dict[str, list[Instrument]] = defaultdict(list)
    spots: dict[str, dict[str, Instrument]] = defaultdict(dict)  # asset -> venue -> best spot
    for inst in instruments:
        if inst.kind is Kind.PERP:
            perps[inst.asset].append(inst)
        else:
            cur = spots[inst.asset].get(inst.venue)
            if cur is None or _quote_rank(inst.quote) < _quote_rank(cur.quote):
                spots[inst.asset][inst.venue] = inst

    out: list[Candidate] = []
    cons = set(params.constructions)
    for asset, ps in perps.items():
        venue_spots = spots.get(asset, {})
        for p in ps:
            for s in venue_spots.values():
                same = s.venue == p.venue
                if not (same or params.cross_venue_spot):
                    continue
                if Construction.SPOT_PERP in cons:
                    out.append(Candidate(Construction.SPOT_PERP, asset, long=s, short=p))
                if Construction.REVERSE in cons and s.venue in params.borrow_venues:
                    out.append(Candidate(Construction.REVERSE, asset, long=p, short=s))
        if Construction.PERP_PERP in cons:
            for a in ps:
                for b in ps:
                    if a.key != b.key:
                        out.append(Candidate(Construction.PERP_PERP, asset, long=a, short=b))
    return out


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    carry_now_hourly: float  # from the latest rates, before costs
    carry_fcst_hourly: float  # forecast mean over the horizon, before costs
    round_trip_cost: float  # fraction of notional
    capital_per_notional: float
    horizon_hours: float

    @property
    def expected_return(self) -> float:
        """Expected net return per $ notional if held for the horizon then closed."""
        return self.carry_fcst_hourly * self.horizon_hours - self.round_trip_cost

    @property
    def net_apr_on_capital(self) -> float:
        return (
            self.expected_return / self.horizon_hours * HOURS_PER_YEAR / self.capital_per_notional
        )

    @property
    def gross_apr_now(self) -> float:
        return self.carry_now_hourly * HOURS_PER_YEAR

    @property
    def fcst_apr_on_capital(self) -> float:
        """Forecast carry APR on capital ignoring costs (used for hold/exit decisions)."""
        return self.carry_fcst_hourly * HOURS_PER_YEAR / self.capital_per_notional

    @property
    def breakeven_hours(self) -> float:
        return breakeven_hours(self.round_trip_cost, self.carry_fcst_hourly)


def score(
    cand: Candidate,
    long_now: float,
    short_now: float,
    long_fcst: float,
    short_fcst: float,
    costs: CostModel,
    params: StrategyParams,
    long_price: float | None = None,
    short_price: float | None = None,
) -> Scored:
    """Score with per-leg hourly rates (spot legs: pass 0). Prices enable per-contract fees."""
    borrow = costs.borrow_hourly(cand)
    return Scored(
        candidate=cand,
        carry_now_hourly=candidate_carry_hourly(cand, long_now, short_now, borrow),
        carry_fcst_hourly=candidate_carry_hourly(cand, long_fcst, short_fcst, borrow),
        round_trip_cost=costs.round_trip_cost(cand, long_price, short_price),
        capital_per_notional=costs.capital_per_notional(cand),
        horizon_hours=params.horizon_hours,
    )


def passes_entry(s: Scored, params: StrategyParams) -> bool:
    return (
        s.net_apr_on_capital >= params.entry_apr
        and s.breakeven_hours <= params.max_breakeven_hours
        and not math.isnan(s.carry_fcst_hourly)
    )


def should_exit(s: Scored, params: StrategyParams, held_hours: float) -> str | None:
    if math.isnan(s.carry_fcst_hourly):
        return "no data"
    if s.fcst_apr_on_capital < params.exit_apr:
        return f"forecast carry {s.fcst_apr_on_capital:.1%} < exit {params.exit_apr:.1%}"
    if params.max_hold_hours is not None and held_hours >= params.max_hold_hours:
        return "max hold"
    return None
