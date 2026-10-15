import math

import pytest
from conftest import perp, spot

from funding_capture.costs import CostModel, VenueFees
from funding_capture.forecast import ForecastParams, StreamingForecaster, horizon_average
from funding_capture.models import Construction
from funding_capture.opportunities import (
    StrategyParams,
    build_candidates,
    passes_entry,
    score,
    should_exit,
)
from funding_capture.rates import (
    candidate_carry_hourly,
    infer_interval_hours,
    parse_interval_hours,
)
from funding_capture.symbols import canonical_asset


@pytest.mark.parametrize(
    "base,expected",
    [
        ("BTC", ("BTC", 1.0)),
        ("1000PEPE", ("PEPE", 1e3)),
        ("kPEPE", ("PEPE", 1e3)),
        ("1000000MOG", ("MOG", 1e6)),
        ("1MBABYDOGE", ("BABYDOGE", 1e6)),
        ("1INCH", ("1INCH", 1.0)),  # a real ticker, not a multiplier
        ("KAVA", ("KAVA", 1.0)),  # uppercase K is part of the name
        ("UBTC", ("BTC", 1.0)),
        ("XBT", ("BTC", 1.0)),
    ],
)
def test_canonical_asset(base, expected):
    assert canonical_asset(base) == expected


def test_intervals():
    assert parse_interval_hours("8h") == 8
    assert parse_interval_hours("60m") == 1
    assert parse_interval_hours("1d") == 24
    assert parse_interval_hours(4) == 4
    assert parse_interval_hours("weird") is None
    h = 3_600_000
    assert infer_interval_hours([0, 8 * h, 16 * h, 24 * h]) == 8
    assert infer_interval_hours([0, h]) is None


def test_carry_signs():
    s, p, q = spot("a"), perp("a"), perp("b")
    sp = build_candidates([s, p], StrategyParams(constructions=(Construction.SPOT_PERP,)))[0]
    # positive funding: the short perp is paid
    assert candidate_carry_hourly(sp, 0.0, 0.001) == pytest.approx(0.001)
    rev = build_candidates(
        [s, p], StrategyParams(constructions=(Construction.REVERSE,), borrow_venues=("a",))
    )[0]
    # negative funding: the long perp is paid, minus borrow
    assert candidate_carry_hourly(rev, -0.001, 0.0, borrow_hourly=0.0002) == pytest.approx(0.0008)
    pp = [
        c
        for c in build_candidates([p, q], StrategyParams(constructions=(Construction.PERP_PERP,)))
        if c.long.venue == "b"
    ][0]
    # long b at 0.0001, short a at 0.0005 -> earn the spread
    assert candidate_carry_hourly(pp, 0.0001, 0.0005) == pytest.approx(0.0004)


def test_build_candidates_counts_and_quote_preference():
    insts = [
        perp("a"),
        perp("b"),
        spot("a"),
        spot("a", quote="USDC"),  # dropped in favour of USDT spot on the same venue
        spot("c"),
        perp("a", asset="ETH"),  # no spot or second perp -> no pairs
    ]
    params = StrategyParams(constructions=(Construction.SPOT_PERP, Construction.PERP_PERP))
    cands = build_candidates(insts, params)
    sp = [c for c in cands if c.construction is Construction.SPOT_PERP]
    pp = [c for c in cands if c.construction is Construction.PERP_PERP]
    assert len(pp) == 2  # a/b in both directions
    assert len(sp) == 4  # 2 perps x 2 spot venues
    assert all(c.long.quote == "USDT" for c in sp)
    same_venue_only = build_candidates(insts, StrategyParams(cross_venue_spot=False))
    assert {(c.long.venue, c.short.venue) for c in same_venue_only if c.long.kind == "spot"} == {
        ("a", "a")
    }


def test_cost_model():
    costs = CostModel(
        fees={"a": VenueFees(spot_taker=0.001, perp_taker=0.0005)},
        slippage_bps=0,
        perp_leverage=2,
    )
    sp = build_candidates([spot("a"), perp("a")], StrategyParams())[0]
    assert costs.round_trip_cost(sp) == pytest.approx(2 * (0.001 + 0.0005))
    assert costs.capital_per_notional(sp) == pytest.approx(1.5)
    costs.unified_margin_credit = 1.0
    assert costs.capital_per_notional(sp) == pytest.approx(1.0)


def test_score_and_rules():
    costs = CostModel(fees={"a": VenueFees(0.001, 0.001, 0.0005, 0.0002)}, slippage_bps=0)
    params = StrategyParams(horizon_hours=100, entry_apr=0.10, max_breakeven_hours=50)
    sp = build_candidates([spot("a"), perp("a")], params)[0]
    hourly = 0.0001  # 87.6% APR gross
    s = score(sp, 0, hourly, 0, hourly, costs, params)
    assert s.round_trip_cost == pytest.approx(0.003)
    assert s.expected_return == pytest.approx(hourly * 100 - 0.003)
    assert s.breakeven_hours == pytest.approx(30)
    assert s.net_apr_on_capital == pytest.approx((0.01 - 0.003) / 100 * 8760 / 1.5)
    assert passes_entry(s, params)
    slow = score(sp, 0, hourly / 4, 0, hourly / 4, costs, params)  # 120h to break even
    assert not passes_entry(slow, params)
    assert should_exit(s, params, 10) is None
    flipped = score(sp, 0, -hourly, 0, -hourly, costs, params)
    assert should_exit(flipped, params, 10)
    assert should_exit(score(sp, 0, math.nan, 0, math.nan, costs, params), params, 1) == "no data"


def test_horizon_average_limits():
    p = ForecastParams(reversion_halflife_hours=24, anchor_hourly=0.0)
    assert horizon_average(1.0, p, 1) == pytest.approx(1.0)  # first hour ~= level
    assert horizon_average(1.0, p, 10_000) < 0.01  # long horizon -> anchor
    no_rev = ForecastParams(reversion_halflife_hours=math.inf)
    assert horizon_average(0.5, no_rev, 1000) == 0.5


def test_streaming_forecaster():
    f = StreamingForecaster(ForecastParams(halflife_hours=1, reversion_halflife_hours=math.inf))
    h = 3_600_000
    f.observe("x", 1.0, 0)
    f.observe("x", 0.0, h)  # one half-life later
    assert f.level("x") == pytest.approx(0.5)
    assert f.level("x", live_hourly=1.0) == pytest.approx(0.75)  # live_weight 0.5
    assert f.forecast("missing", 10) is None
    g = StreamingForecaster()
    g.load_dict(f.to_dict())
    assert g.level("x") == pytest.approx(0.5)
