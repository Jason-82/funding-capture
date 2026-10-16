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

    def new_engine(self, venue_cls=PaperVenue):
        self.va, self.vb = venue_cls(self.da, FEES), venue_cls(self.db, FEES)
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


def _quit_after_each_step(w, monkeypatch, first=1):
    real, calls = w.engine.step, []

    def step():
        calls.append(1)
        rep = real()
        if len(calls) >= first:
            (w.tmp_path / "QUIT").touch()  # as if the user touched it during this step
        return rep

    monkeypatch.setattr(w.engine, "step", step)
    return calls


def test_quit_file_ends_run_between_steps(tmp_path, clock, monkeypatch):
    w = World(tmp_path, clock, loop_seconds=0.01)
    (tmp_path / "QUIT").touch()  # left over from an earlier stop: must not end this run
    calls = _quit_after_each_step(w, monkeypatch, first=2)
    w.engine.run(max_steps=5)
    assert len(calls) == 2  # step 2 finished, then the QUIT made during it ended the run
    assert not (tmp_path / "QUIT").exists()  # removed, so the next start runs normally


def test_second_engine_on_the_same_state_is_refused(tmp_path, clock):
    from funding_capture.state import StateLocked, lock_state_dir

    w = World(tmp_path, clock, loop_seconds=0.01)
    with lock_state_dir(tmp_path), pytest.raises(StateLocked):
        w.engine.run(max_steps=1)
    w.engine.run(max_steps=1)  # released again


def test_serious_halt_survives_stop_file_and_stale_data(tmp_path, clock):
    from funding_capture.engine import StepReport
    from funding_capture.risk import Action, RiskAlert

    w = World(tmp_path, clock)
    serious = "unhedged residual or execution error opening X"
    w.engine.entries_halted = serious
    (tmp_path / "STOP").touch()
    w.engine.step()
    (tmp_path / "STOP").unlink()
    clock.advance(minutes=1)
    w.touch_quotes()
    assert w.engine.step().opened == [] and w.engine.entries_halted == serious
    stale, fresh = StepReport(), StepReport()
    stale.alerts = [RiskAlert(Action.HALT, "data", "stale data from a")]
    w.engine._handle_alerts(stale, clock.ms)
    w.engine._handle_alerts(fresh, clock.ms)  # data back
    assert w.engine.entries_halted == serious
    w.engine.entries_halted = None  # a soft halt still lifts by itself
    w.engine._handle_alerts(stale, clock.ms)
    assert w.engine.entries_halted == "stale market data"
    w.engine._handle_alerts(fresh, clock.ms)
    assert w.engine.entries_halted is None


def test_unwritable_state_blocks_entries_and_quit(tmp_path, clock, monkeypatch, caplog):
    import funding_capture.engine as engine_mod

    def locked(path, data):
        raise PermissionError(13, "in use by another process", str(path))

    w = World(tmp_path, clock, loop_seconds=0.01)
    monkeypatch.setattr(engine_mod, "save_json", locked)
    (tmp_path / "STOP").touch()
    w.engine.step()  # opens nothing (STOP); its save fails
    (tmp_path / "STOP").unlink()
    assert w.engine._save_failed and "cannot write" in caplog.text
    clock.advance(minutes=1)
    w.touch_quotes()
    assert w.engine.step().opened == []  # would open, but the last save failed
    from funding_capture.state import save_json as real_save

    real_step, calls = w.engine.step, []

    def step():
        calls.append(1)
        if len(calls) == 3:  # the other program lets go of the file
            monkeypatch.setattr(engine_mod, "save_json", real_save)
        rep = real_step()
        if len(calls) == 1:
            (tmp_path / "QUIT").touch()  # touched once, while the file can't be written
        return rep

    monkeypatch.setattr(w.engine, "step", step)
    w.engine.run(max_steps=10)
    assert len(calls) == 3 and "QUIT waits" in caplog.text  # then exits without a 2nd QUIT
    assert not w.engine.positions and not w.engine._save_failed
    assert (tmp_path / "engine.json").exists()
    monkeypatch.undo()
    clock.advance(minutes=1)
    w.touch_quotes()
    assert w.engine.step().opened  # entries resume


def test_cooldowns_survive_a_restart(tmp_path, clock):
    w = World(tmp_path, clock)
    w.engine._cooldown_until = {"a:BTC": clock.ms + HOUR, "a:OLD": clock.ms - 1}
    w.engine._venue_blocked_until = {"b": clock.ms + HOUR}
    w.engine._save()
    again = w.new_engine()
    assert again._cooldown_until == {"a:BTC": clock.ms + HOUR}
    assert again._venue_blocked_until == {"b": clock.ms + HOUR}


def test_each_summary_line_is_logged_with_a_time(tmp_path, clock, caplog):
    import logging

    w = World(tmp_path, clock)
    with caplog.at_level(logging.INFO, logger="funding_capture"):
        w.engine._log_step(w.engine.step())
    assert caplog.records[-1].getMessage().startswith("realised net P&L")
    assert all("\n" not in r.getMessage() for r in caplog.records)


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


class NoFundingHistoryVenue(PaperVenue):
    """Like Coinbase US futures: trades fine but can't report funding paid."""

    reports_funding = False


def test_engine_estimates_funding_when_venue_cannot_report_it(tmp_path, clock):
    w = World(tmp_path, clock)
    w.engine = w.new_engine(NoFundingHistoryVenue)
    w.engine.step()
    pos = next(iter(w.engine.positions.values()))
    clock.advance(hours=8, minutes=1)
    w.set_funding(0.0005, 0.0001, next_ts=T0 + 16 * HOUR)
    w.touch_quotes()
    w.engine.step()
    w.engine.step()  # a second loop must not double count
    # valued at the leg's mid (100.01) rather than the venue's mark (100.00)
    assert pos.funding_usd == pytest.approx(0.0004 * pos.qty * 100.01)
    assert "funding estimated from published rates" in pos.notes


def _open_one(w):
    rep = w.engine.step()
    assert len(rep.opened) == 1
    return w.engine.positions[rep.opened[0]]


def _perp_venue(w, pos):
    inst = pos.candidate.short if pos.candidate.short.is_perp else pos.candidate.long
    return inst, {"a": w.va, "b": w.vb}[inst.venue]


def test_failed_position_read_after_an_open_is_not_taken_as_no_position(
    tmp_path, clock, monkeypatch
):
    w = World(tmp_path, clock)
    pos = _open_one(w)
    inst, venue = _perp_venue(w, pos)

    def down():
        raise TimeoutError("timed out")

    monkeypatch.setattr(venue, "fetch_perp_positions", down)
    clock.advance(minutes=1)
    w.touch_quotes()
    rep = w.engine.step()
    assert pos.id in w.engine.positions and rep.closed == []  # not "flattened"
    assert w.engine.entries_halted == "stale market data"  # soft: lifts next step
    monkeypatch.undo()
    clock.advance(minutes=1)
    w.touch_quotes()
    w.engine.step()
    assert pos.id in w.engine.positions and w.engine.entries_halted is None


def test_untracked_perp_position_halts_entries(tmp_path, clock):
    w = World(tmp_path, clock)
    w.engine.executor.trade_one(w.p_b, "sell", 10, False)  # a short no pair accounts for
    rep = w.engine.step()
    assert rep.opened == []
    assert w.engine.entries_halted.startswith("untracked position on b:")


def test_failed_close_waits_before_trying_again(tmp_path, clock, monkeypatch):
    w = World(tmp_path, clock)
    pos = _open_one(w)
    calls = []

    def broken(*a, **k):
        calls.append(1)
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(w.engine.executor, "close", broken)
    (tmp_path / "FLATTEN").touch()
    for _ in range(5):
        clock.advance(minutes=1)
        w.touch_quotes()
        w.engine.step()
    assert len(calls) == 1 and pos.id in w.engine.positions  # one try, not one a minute
    clock.advance(minutes=10)
    w.touch_quotes()
    w.engine.step()
    assert len(calls) == 2


def test_legs_smaller_than_recorded_are_resized_then_closed(tmp_path, clock):
    w = World(tmp_path, clock)
    pos = _open_one(w)
    c = pos.candidate
    cut = pos.qty * 0.05  # as if stopped in the middle of a close: both legs 5% smaller
    w.engine.executor.trade_one(c.long, "sell", cut, c.long.is_perp)
    w.engine.executor.trade_one(c.short, "buy", cut, c.short.is_perp)
    clock.advance(minutes=1)
    w.touch_quotes()
    w.engine.step()
    assert pos.qty == pytest.approx(w.engine.positions[pos.id].qty)
    assert any("resized to holdings" in n for n in pos.notes)
    (tmp_path / "FLATTEN").touch()
    clock.advance(minutes=1)
    w.touch_quotes()
    rep = w.engine.step()
    assert rep.closed == [pos.id]
    held = {**w.va.fetch_perp_positions(), **w.vb.fetch_perp_positions()}
    assert held == {}


def test_quit_waits_for_flatten_to_finish(tmp_path, clock, monkeypatch):
    w = World(tmp_path, clock, loop_seconds=0.01)
    real, calls = w.engine.step, []

    def step():
        calls.append(1)
        rep = real()
        if len(calls) == 1:  # "close everything, then stop", typed in one go
            (tmp_path / "FLATTEN").touch()
            (tmp_path / "QUIT").touch()
        clock.advance(minutes=1)
        w.touch_quotes()
        return rep

    monkeypatch.setattr(w.engine, "step", step)
    w.engine.run(max_steps=5)
    assert len(calls) == 2 and not w.engine.positions  # closed in step 2, then exited


def test_step_that_trades_then_fails_is_still_saved(tmp_path, clock, monkeypatch):
    from funding_capture.state import load_json

    w = World(tmp_path, clock, loop_seconds=0.01)
    real = w.engine.step

    def step():
        real()  # opens a pair and saves
        w.engine.positions[next(iter(w.engine.positions))].notes.append("after the save")
        raise ConnectionError("a later call in the step failed")

    monkeypatch.setattr(w.engine, "step", step)
    w.engine.run(max_steps=1)  # `fcap paper --steps 1`: returns right after the step
    saved = load_json(tmp_path / "engine.json")
    assert saved["positions"][0]["notes"][-1] == "after the save"


def test_no_quotes_before_an_order_is_not_a_serious_halt(tmp_path, clock, monkeypatch):
    w = World(tmp_path, clock)

    def down(inst):
        raise TimeoutError("timed out")

    monkeypatch.setattr(w.va, "fetch_touch", down)
    rep = w.engine.step()
    assert rep.opened == [] and w.engine.entries_halted is None


def test_pair_traded_this_step_is_not_judged_on_positions_read_before(tmp_path, clock, monkeypatch):
    from funding_capture.execution import ExecutionReport

    w = World(tmp_path, clock)
    spot_perp = Construction.SPOT_PERP  # a stale perp reading shows up as drift here
    w.engine.candidates = [c for c in w.engine.candidates if c.construction is spot_perp]
    pos = _open_one(w)
    ex = w.engine.executor

    def half_close(cand, qty, urgent=False):
        rep = ExecutionReport()
        rep.fills.append(ex.trade_one(cand.long, "sell", qty / 2, cand.long.is_perp))
        rep.fills.append(ex.trade_one(cand.short, "buy", qty / 2, cand.short.is_perp))
        return rep

    monkeypatch.setattr(ex, "close", half_close)
    (tmp_path / "FLATTEN").touch()
    clock.advance(minutes=1)
    w.touch_quotes()
    w.engine.step()  # FLATTEN closes half; the positions read at the start show it all
    assert pos.id in w.engine.positions
    assert not any(n.startswith("flattened") for n in pos.notes)
