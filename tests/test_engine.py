import math

import pytest
from conftest import HOUR, T0, FakeVenue, perp, spot

from funding_capture.costs import CostModel, VenueFees
from funding_capture.engine import Engine, EngineConfig
from funding_capture.execution import PairExecutor
from funding_capture.forecast import ForecastParams
from funding_capture.models import Construction
from funding_capture.opportunities import StrategyParams
from funding_capture.risk import RiskLimits
from funding_capture.venues.paper import PaperVenue

FEES = VenueFees(spot_taker=0.001, spot_maker=0.001, perp_taker=0.0005, perp_maker=0.0002)


class World:
    """Two venues: a has BTC spot + perp, b has a BTC perp."""

    def __init__(self, tmp_path, clock, **cfg):
        self.s_a, self.p_a, self.p_b = spot("a"), perp("a"), perp("b")
        self.da = FakeVenue("a", [self.s_a, self.p_a])
        self.db = FakeVenue("b", [self.p_b])
        self.clock = clock
        for d, inst in ((self.da, self.s_a), (self.da, self.p_a), (self.db, self.p_b)):
            d.set_quote(inst, 100.0, 100.02)
        self.set_funding(0.0005, 0.0001, next_ts=T0 + 8 * HOUR)
        self.tmp_path = tmp_path
        self.cfg = EngineConfig(
            capital_usd=10_000,
            max_positions=2,
            max_position_usd=4_000,
            min_volume_usd=0,
            state_dir=str(tmp_path),
            **cfg,
        )
        self.engine = self.new_engine()

    def set_funding(self, a_rate, b_rate, next_ts):
        self.da.set_funding(self.p_a, a_rate, next_ts=next_ts, mark=100.0)
        self.db.set_funding(self.p_b, b_rate, next_ts=next_ts, mark=100.0)

    def touch_quotes(self):
        for d in (self.da, self.db):
            for k, q in list(d.quotes.items()):
                d.quotes[k] = type(q)(
                    q.instrument,
                    q.bid,
                    q.ask,
                    q.bid_size,
                    q.ask_size,
                    self.clock.ms,
                    q.volume_usd_24h,
                )

    def new_engine(self):
        self.va, self.vb = PaperVenue(self.da, FEES), PaperVenue(self.db, FEES)
        venues = {"a": self.va, "b": self.vb}
        strategy = StrategyParams(
            constructions=(Construction.SPOT_PERP, Construction.PERP_PERP),
            entry_apr=0.10,
            exit_apr=0.0,
            max_breakeven_hours=200,
            forecast=ForecastParams(halflife_hours=8, reversion_halflife_hours=math.inf),
        )
        costs = CostModel(fees={"a": FEES, "b": FEES}, slippage_bps=0)
        eng = Engine(
            venues, strategy, costs, PairExecutor(venues), RiskLimits(), self.cfg, clock=self.clock
        )
        eng.bootstrap()
        return eng


def test_opens_best_pair_collects_funding_and_exits_on_flip(tmp_path, clock):
    w = World(tmp_path, clock)
    rep = w.engine.step()
    assert len(rep.opened) == 1  # one per asset
    pos = next(iter(w.engine.positions.values()))
    c = pos.candidate
    assert c.construction is Construction.PERP_PERP
    assert (c.long.venue, c.short.venue) == ("b", "a")
    assert pos.entry_notional == pytest.approx(4_000, rel=0.01)

    # cross the first settlement
    clock.advance(hours=8, minutes=1)
    w.set_funding(0.0005, 0.0001, next_ts=T0 + 16 * HOUR)
    w.touch_quotes()
    w.engine.step()
    expected = 0.0004 * pos.qty * 100.0  # short a receives 5bp, long b pays 1bp
    assert pos.funding_usd == pytest.approx(expected)
    assert w.engine.positions  # still open

    # funding flips: venue a now pays shorts negative funding
    clock.advance(minutes=1)
    w.set_funding(-0.0010, 0.0001, next_ts=T0 + 16 * HOUR)
    w.touch_quotes()
    rep = w.engine.step()
    assert rep.closed == [pos.id]
    # the flipped market is now attractive the other way round
    for p in w.engine.positions.values():
        assert (p.candidate.long.venue, p.candidate.short.venue) != ("b", "a")
    closed = w.engine.closed[-1]
    assert closed.exit_fees_usd > 0
    assert closed.net_pnl_usd == pytest.approx(
        closed.funding_usd
        - closed.entry_fees_usd
        - closed.exit_fees_usd
        + closed.realized_price_usd
    )
    held = {**w.va.fetch_perp_positions(), **w.vb.fetch_perp_positions()}
    reopened = {
        i.key for p in w.engine.positions.values() for i in (p.candidate.long, p.candidate.short)
    }
    assert set(held) == reopened  # nothing left over from the closed pair


def test_settlement_guard_skips_pair_that_would_pay_imminently(tmp_path, clock):
    w = World(tmp_path, clock, settlement_guard_minutes=10)
    # a settles hourly at 1bp/h (next in 50 min); b settles 8-hourly at 2bp (next in 5 min).
    # Best on paper is long b / short a, but b's leg would *pay* 2bp in five minutes.
    w.da.set_funding(w.p_a, 0.0001, interval=1, next_ts=clock.ms + 50 * 60_000, mark=100.0)
    w.db.set_funding(w.p_b, 0.0002, interval=8, next_ts=clock.ms + 5 * 60_000, mark=100.0)
    rep = w.engine.step()
    best = rep.top[0].candidate
    assert (best.construction, best.long.venue, best.short.venue) == (
        Construction.PERP_PERP,
        "b",
        "a",
    )
    opened = w.engine.positions[rep.opened[0]].candidate
    assert opened.construction is Construction.SPOT_PERP  # next best, nothing imminent


def test_stop_file_blocks_entries_and_flatten_closes(tmp_path, clock):
    w = World(tmp_path, clock)
    (tmp_path / "STOP").touch()
    assert w.engine.step().opened == []
    (tmp_path / "STOP").unlink()
    w.touch_quotes()
    assert w.engine.step().opened
    (tmp_path / "FLATTEN").touch()
    clock.advance(minutes=1)
    w.touch_quotes()
    rep = w.engine.step()
    assert rep.closed and not w.engine.positions
    assert w.engine.entries_halted


def test_stale_venue_halts_entries(tmp_path, clock):
    w = World(tmp_path, clock)
    w.db.fail = True
    w.engine.step()
    clock.advance(minutes=5)
    rep = w.engine.step()
    assert rep.opened == []
    assert w.engine.entries_halted == "stale market data"


def test_state_survives_restart(tmp_path, clock):
    w = World(tmp_path, clock)
    w.engine.step()
    pid = next(iter(w.engine.positions))
    qty_a = w.va.state.books[w.p_a.key].qty
    restarted = w.new_engine()  # fresh objects, same state_dir
    assert list(restarted.positions) == [pid]
    assert w.va.state.books[w.p_a.key].qty == qty_a
    # restored books are reconciled by risk checks without spurious alerts
    w.touch_quotes()
    rep = restarted.step()
    assert [a for a in rep.alerts if a.position_id == pid] == []


def test_risk_close_when_a_leg_vanishes(tmp_path, clock):
    w = World(tmp_path, clock)
    w.engine.step()
    pos = next(iter(w.engine.positions.values()))
    # simulate ADL: venue a silently closes our short perp
    w.va.state.books[w.p_a.key].qty = 0.0
    clock.advance(minutes=1)
    w.touch_quotes()
    rep = w.engine.step()
    assert pos.id in rep.closed
    assert any(a.check == "delta" for a in rep.alerts)
    assert w.vb.fetch_perp_positions() == {}  # the surviving leg was closed, not left naked
    assert not w.engine.entries_halted


def test_funding_accumulates_once_per_settlement(tmp_path, clock):
    w = World(tmp_path, clock, funding_refresh_minutes=0)
    w.engine.step()
    pos = next(iter(w.engine.positions.values()))
    for k in (1, 2):
        clock.advance(hours=8)
        w.set_funding(0.0005, 0.0001, next_ts=T0 + (k + 1) * 8 * HOUR)
        for _ in range(3):  # several loops between settlements must not double count
            clock.advance(minutes=1)
            w.touch_quotes()
            w.engine.step()
    assert pos.funding_usd == pytest.approx(2 * 0.0004 * pos.qty * 100.0)
