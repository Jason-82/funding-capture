import math

import pandas as pd
import pytest
from conftest import HOUR, T0, perp, settlement_frame, spot

from funding_capture.backtest import BacktestConfig, Backtester, sweep
from funding_capture.costs import CostModel, VenueFees
from funding_capture.forecast import ForecastParams
from funding_capture.models import Construction
from funding_capture.opportunities import StrategyParams

FEES = {"a": VenueFees(0.001, 0.001, 0.0005, 0.0002), "b": VenueFees(0.001, 0.001, 0.0005, 0.0002)}
NO_REVERSION = ForecastParams(halflife_hours=8, reversion_halflife_hours=math.inf)


def _bt(series, instruments, params=None, **cfg):
    params = params or StrategyParams(
        constructions=(Construction.PERP_PERP,),
        entry_apr=0.05,
        exit_apr=0.0,
        max_breakeven_hours=1000,
        forecast=NO_REVERSION,
    )
    costs = CostModel(fees=FEES, slippage_bps=0, perp_leverage=2)
    cfg = {"max_leg_abs_apr": None, **cfg}
    config = BacktestConfig(capital_usd=10_000, max_positions=1, warmup_hours=24, **cfg)
    return Backtester(instruments, series, params, costs, config)


def test_constant_spread_pnl_matches_hand_calculation():
    days = 30
    pa, pb = perp("a"), perp("b")
    series = {
        pa.key: settlement_frame(T0, days * 24, 8, lambda i: 0.0005),
        pb.key: settlement_frame(T0, days * 24, 8, lambda i: 0.0001),
    }
    res = _bt(series, [pa, pb]).run()
    tr = res.trades
    assert len(tr) == 1
    t = tr.iloc[0]
    assert t["long"] == "b:perp" and t["short"] == "a:perp"  # long the cheap side
    # one position using all capital: perp-perp at 2x -> notional = capital
    assert t["notional_usd"] == pytest.approx(10_000)
    # costs: 4 perp taker fills at 5 bps
    assert t["costs_usd"] == pytest.approx(10_000 * 4 * 0.0005)
    # funding: 0.04% of notional per settlement held after entry
    entered_hour = (pd.Timestamp(t["opened"], tz="UTC").value // 1_000_000 - T0) // HOUR
    last_hour = days * 24
    settlements = sum(1 for k in range(1, days * 3 + 1) if entered_hour < k * 8 <= last_hour)
    assert t["funding_usd"] == pytest.approx(10_000 * 0.0004 * settlements)
    assert res.summary["net_pnl_usd"] == pytest.approx(t["net_usd"])
    assert res.equity.iloc[-1] == pytest.approx(10_000 + t["net_usd"])


def test_no_lookahead_on_single_spike():
    """A one-off spike must not be captured: we only learn of it after it settles."""
    pa, pb = perp("a"), perp("b")
    spike_at = 20
    series = {
        pa.key: settlement_frame(T0, 30 * 24, 8, lambda i: 0.05 if i == spike_at else 0.0),
        pb.key: settlement_frame(T0, 30 * 24, 8, lambda i: 0.0),
    }
    res = _bt(series, [pa, pb]).run()
    assert res.summary["funding_usd"] <= 1e-9  # whatever it did, it never earned the spike


def test_exits_when_funding_flips():
    pa, pb = perp("a"), perp("b")
    flip = 45  # settlement index where the spread turns against us
    series = {
        pa.key: settlement_frame(T0, 40 * 24, 8, lambda i: 0.0005 if i < flip else -0.0005),
        pb.key: settlement_frame(T0, 40 * 24, 8, lambda i: 0.0),
    }
    res = _bt(series, [pa, pb]).run()
    first = res.trades.iloc[0]
    assert first["short"] == "a:perp"
    assert first["exit_reason"] == "carry below exit"
    closed_hour = (pd.Timestamp(first["closed"], tz="UTC").value // 1_000_000 - T0) // HOUR
    assert flip * 8 < closed_hour < (flip + 6) * 8  # left within a few settlements of the flip


def test_spot_perp_and_capital_efficiency_ranking():
    pa = perp("a")
    series = {pa.key: settlement_frame(T0, 20 * 24, 8, lambda i: 0.0003)}
    params = StrategyParams(
        constructions=(Construction.SPOT_PERP,),
        entry_apr=0.05,
        max_breakeven_hours=1000,
        forecast=NO_REVERSION,
    )
    res = _bt(series, [pa, spot("a")], params).run()
    t = res.trades.iloc[0]
    assert t["construction"] == "spot_perp"
    # long spot (1.0) + short perp margin at 2x (0.5) -> notional = capital / 1.5
    assert t["notional_usd"] == pytest.approx(10_000 / 1.5)


def test_leg_apr_filter_blocks_squeezes():
    pa, pb = perp("a"), perp("b")
    series = {
        pa.key: settlement_frame(T0, 20 * 24, 8, lambda i: 0.01),  # ~1100% APR
        pb.key: settlement_frame(T0, 20 * 24, 8, lambda i: 0.0),
    }
    assert _bt(series, [pa, pb], max_leg_abs_apr=5.0).run().summary["trades"] == 0
    assert _bt(series, [pa, pb], max_leg_abs_apr=None).run().summary["trades"] == 1


def test_mixed_intervals_normalised():
    """1h venue at 0.01%/h vs 8h venue at 0.01%/8h: spread is 0.00875%/h."""
    pa, pb = perp("a"), perp("b")
    series = {
        pa.key: settlement_frame(T0, 20 * 24, 1, lambda i: 0.0001),
        pb.key: settlement_frame(T0, 20 * 24, 8, lambda i: 0.0001),
    }
    bt = _bt(series, [pa, pb])
    assert bt.known[-1, bt.col_of[pa.key]] == pytest.approx(0.0001)
    assert bt.known[-1, bt.col_of[pb.key]] == pytest.approx(0.0001 / 8)
    t = bt.run().trades.iloc[0]
    assert t["short"] == "a:perp"


def test_sweep_runs():
    pa, pb = perp("a"), perp("b")
    series = {
        pa.key: settlement_frame(T0, 40 * 24, 8, lambda i: 0.0004 + 0.0002 * math.sin(i / 5)),
        pb.key: settlement_frame(T0, 40 * 24, 8, lambda i: 0.0001),
    }
    df = sweep(_bt(series, [pa, pb]), {"entry_apr": [0.05, 0.5], "exit_apr": [0.0]})
    assert len(df) == 2
    assert {"train_apr_pct", "test_apr_pct"} <= set(df.columns)


def test_settlement_intervals_follow_changes_and_survive_holes():
    from funding_capture.backtest import _settlements

    ts = [T0 + h * HOUR for h in (8, 16, 24, 28, 32, 36, 60, 64)]  # 8h, then 4h, a hole, 4h
    df = pd.DataFrame({"ts_ms": ts, "rate": [0.0008] * len(ts)})
    s = _settlements(df, "binance")
    assert s["interval"].tolist() == [8, 8, 8, 4, 4, 4, 4, 4]
    assert s["hourly"].iloc[0] == pytest.approx(0.0001)
    assert s["hourly"].iloc[-1] == pytest.approx(0.0002)
