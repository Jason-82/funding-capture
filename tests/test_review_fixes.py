"""Regression tests for defects found in the adversarial review."""

import ccxt
import pytest
from conftest import T0, FakeVenue, perp, spot

from funding_capture.costs import VenueFees
from funding_capture.execution import ExecutionParams, PairExecutor
from funding_capture.models import Candidate, Construction, Instrument, Kind
from funding_capture.venues.ccxt_venue import CcxtVenue
from funding_capture.venues.paper import PaperVenue

SPOT_FEES = VenueFees(spot_taker=0.004, spot_maker=0.0025)
PERP_FEES = VenueFees(perp_taker=0.00045, perp_maker=0.00015)


class Calls(list):
    def log(self, *item):
        self.append(item)


def _maker_sim(script=None, hedge_cls=PaperVenue, min_notional=0.0, **params):
    """Kraken-like spot (maker) + perp hedge; ``script`` moves the spot book per poll."""
    s, p = spot("kraken", quote="USD"), perp("hl", quote="USDC")
    dk, dh = FakeVenue("kraken", [s]), FakeVenue("hl", [p], lot=0.00001)
    dk.set_quote(s, 99.9, 100.1, size=1e6)
    dh.set_quote(p, 100.0, 100.05, size=1e6)
    dh.min_notional = lambda inst: min_notional
    vk, vh = PaperVenue(dk, SPOT_FEES), hedge_cls(dh, PERP_FEES)
    vk.load_instruments()
    vh.load_instruments()
    state = {"t": 0.0, "polls": 0}

    def sleep(dt):
        state["t"] += dt
        state["polls"] += 1
        if script and state["polls"] in script:
            dk.set_quote(s, *script[state["polls"]], size=1e6)

    ex = PairExecutor(
        {"kraken": vk, "hl": vh},
        ExecutionParams(maker_timeout_s=30, maker_poll_s=2, **params),
        maker_venues=("kraken",),
        sleep=sleep,
        clock=lambda: state["t"],
    )
    return ex, Candidate(Construction.SPOT_PERP, "BTC", long=s, short=p), vk, vh, dk, state


# ------------------------------------------------------------------ execution safety
def test_hedge_venue_error_mid_maker_is_contained():
    class Exploding(PaperVenue):
        def place_ioc(self, *a, **k):
            raise RuntimeError("coinbase order rejected: INSUFFICIENT_FUND")

    ex, cand, vk, vh, dk, _ = _maker_sim(script={1: (99.7, 99.9)}, hedge_cls=Exploding)
    rep = ex.open(cand, 900)  # must not raise
    assert rep.unsafe and not rep.complete
    assert any("execution error" in m for m in rep.messages)
    assert all(not o.is_open for o in vk._resting.values())  # nothing left resting
    assert vk.fetch_spot_balance(cand.long) == pytest.approx(0, abs=1e-9)  # unwound


def test_engine_halts_when_an_execution_is_unsafe(tmp_path):
    from test_engine import World

    class Exploding(PaperVenue):
        def place_ioc(self, inst, *a, **k):
            if inst.venue == "b":
                raise RuntimeError("venue b down")
            return super().place_ioc(inst, *a, **k)

    class Clock:
        ms = T0

        def __call__(self):
            return self.ms / 1000

    w = World(tmp_path, Clock())
    w.engine = w.new_engine(Exploding)
    rep = w.engine.step()
    assert rep.opened == []
    assert w.engine.entries_halted and "execution error" in w.engine.entries_halted


def test_hedges_respect_minimum_order_value():
    # $8 partial fill can't be hedged on a $10-minimum venue: it is carried, not sent
    class Partial(PaperVenue):
        def place_post_only(self, inst, side, qty, price, reduce_only=False):
            self._po = [0.08, 0.30]
            return super().place_post_only(inst, side, qty, price, reduce_only)

        def fetch_order_state(self, inst, oid):
            from funding_capture.models import OrderState

            o = self._resting[oid]
            if self._po:
                step = self._po.pop(0)
                self._apply(self._book(inst), step, o.price)
                o.filled += step
                o.is_open = bool(self._po)
            return OrderState(oid, o.filled, o.price, 0.0, o.is_open)

    s, p = spot("kraken", quote="USD"), perp("hl", quote="USDC")
    dk, dh = FakeVenue("kraken", [s]), FakeVenue("hl", [p], lot=0.00001)
    dk.set_quote(s, 99.9, 100.1)
    dh.set_quote(p, 100.0, 100.05)
    dh.min_notional = lambda inst: 10.0
    vk, vh = Partial(dk, SPOT_FEES), PaperVenue(dh, PERP_FEES)
    vk.load_instruments()
    vh.load_instruments()
    sent = []
    orig = vh.place_ioc
    vh.place_ioc = lambda inst, side, qty, *a, **k: (
        sent.append(qty) or orig(inst, side, qty, *a, **k)
    )
    ex = PairExecutor(
        {"kraken": vk, "hl": vh}, ExecutionParams(), ("kraken",), sleep=lambda _: None
    )
    rep = ex.open(Candidate(Construction.SPOT_PERP, "BTC", long=s, short=p), 38.0)
    assert sent == [pytest.approx(0.38)]  # one hedge once the total was worth sending
    assert rep.complete and not rep.unsafe


def test_maker_timeout_is_one_budget_across_requotes():
    # the market drifts up 10 bps every poll: endless re-quotes unless the budget is shared
    script = {i: (99.9 * (1 + 0.001 * i), 100.1 * (1 + 0.001 * i)) for i in range(1, 500)}
    ex, cand, *_rest, state = _maker_sim(script=script, maker_requote_bps=5)
    rep = ex.open(cand, 900)
    assert any("timed out" in m for m in rep.messages)
    assert state["t"] <= 30 + 2 * 2  # timeout plus at most a couple of polls


def test_unwind_happens_only_after_our_resting_order_is_cancelled():
    from funding_capture.models import OrderState

    calls = Calls()

    class PartialResting(PaperVenue):
        """Our buy fills 0.3 and keeps resting at the front of the bid."""

        def fetch_order_state(self, inst, oid):
            o = self._resting[oid]
            if o.is_open and o.filled == 0:
                self._apply(self._book(inst), 0.3, o.price)
                o.filled = 0.3
            return OrderState(oid, o.filled, o.price, 0.0, o.is_open)

        def cancel_order(self, inst, oid):
            calls.log("cancel")
            self._resting[oid].is_open = False
            return self.fetch_order_state(inst, oid)

        def place_ioc(self, inst, side, *a, **k):
            calls.log("ioc", side)
            return super().place_ioc(inst, side, *a, **k)

    class Refuse(PaperVenue):
        def place_ioc(self, inst, side, qty, limit, reduce_only=False):
            return super().place_ioc(inst, side, qty, 1e9 if side == "sell" else limit)

    s, p = spot("kraken", quote="USD"), perp("hl", quote="USDC")
    dk, dh = FakeVenue("kraken", [s]), FakeVenue("hl", [p])
    dk.set_quote(s, 99.9, 100.1)
    dh.set_quote(p, 100.0, 100.05)
    vk, vh = PartialResting(dk, SPOT_FEES), Refuse(dh, PERP_FEES)
    vk.load_instruments()
    vh.load_instruments()
    ex = PairExecutor(
        {"kraken": vk, "hl": vh}, ExecutionParams(), ("kraken",), sleep=lambda _: None
    )
    rep = ex.open(Candidate(Construction.SPOT_PERP, "BTC", long=s, short=p), 900)
    assert not rep.complete and any("hedge failed" in m for m in rep.messages)
    # the unwind (selling the 0.3 back) only after our resting buy was cancelled, so it
    # can't run into our own order (self-trade prevention would kill it)
    assert calls.index(("cancel",)) < calls.index(("ioc", "sell"))
    assert vk.fetch_spot_balance(s) == pytest.approx(0, abs=1e-9)


# ------------------------------------------------------------------ venues
def test_lighter_is_scan_only_and_has_funding_estimated():
    v = CcxtVenue("lighter", exchange=ccxt.lighter())
    assert v.can_trade is False and v.reports_funding is False
    with pytest.raises(RuntimeError, match="cannot confirm fills"):
        v.place_ioc(perp("lighter"), "buy", 1, 1)
    hl = CcxtVenue("hyperliquid", exchange=ccxt.hyperliquid())
    assert hl.can_trade and hl.reports_funding


def test_binance_history_pages_without_8h_stepping():
    ex = ccxt.binance()
    v = CcxtVenue("binance", exchange=ex)
    seen = []

    def hist(symbol, since, limit, params):
        seen.append((since, limit, dict(params)))
        h = 3_600_000
        first = -(-since // (4 * h)) * 4 * h  # settlements sit on the 4h grid
        rows = [{"timestamp": first + k * 4 * h, "fundingRate": 1e-4} for k in range(limit)]
        return [r for r in rows if r["timestamp"] <= 2_200 * 4 * h]

    ex.fetch_funding_rate_history = hist
    rows = v.fetch_funding_history(perp("binance"), 0, until_ms=2_200 * 4 * 3_600_000)
    assert len(rows) == 2_201  # a 4h market over the whole window, no holes
    assert all(limit == 1000 and "paginate" not in p for _, limit, p in seen)


def test_paper_charges_per_contract_minimum():
    shib = Instrument("coinbase", "SHP-20DEC30-CDE", "SHIB", Kind.PERP, "USD", 1000, 10000)
    data = FakeVenue("coinbase", [shib], lot=10_000_000)
    data.set_quote(shib, 0.00553 / 1000, 0.00554 / 1000)
    v = PaperVenue(data, VenueFees(perp_taker=0.0002, perp_min_fee_per_contract=0.20))
    v.load_instruments()
    f = v.place_ioc(shib, "sell", 180_000_000, 0.0055 / 1000)  # 18 contracts, ~$995
    assert f.fee_usd == pytest.approx(18 * 0.20)


# ------------------------------------------------------------------ engine
def test_tradeable_uses_the_actual_maker_leg(tmp_path):
    from test_engine import World

    class Clock:
        ms = T0

        def __call__(self):
            return self.ms / 1000

    w = World(tmp_path, Clock())
    eng = w.engine
    eng.costs.maker_venues = ("a", "b")
    eng.cfg = eng.cfg.__class__(**{**eng.cfg.__dict__, "max_spread_bps": 25})
    eng.refresh(T0)
    pp = next(c for c in eng.candidates if c.construction is Construction.PERP_PERP)
    w.da.set_quote(w.p_a, 99.6, 100.4)  # 80 bps on a perp that would be taken with IOC
    w.db.set_quote(w.p_b, 99.6, 100.4)
    eng.refresh(T0)
    assert eng.costs.maker_legs(pp) == (False, True)  # one passive leg per pair
    assert eng.tradeable(pp, T0) is False  # the IOC leg's 80 bps spread is not excused


def test_low_account_margin_blocks_new_entries(tmp_path):
    from test_engine import World

    class Thin(PaperVenue):
        def margin_buffer(self):
            return 0.10 if any(b.qty for b in self.state.books.values()) else 0.90

    class Clock:
        ms = T0

        def __call__(self):
            return self.ms / 1000

    clock = Clock()
    w = World(tmp_path, clock)
    w.engine = w.new_engine(Thin)
    assert w.engine.step().opened
    clock.ms += 60_000
    w.touch_quotes()
    rep = w.engine.step()
    assert rep.closed and rep.opened == []  # closes, and does not reopen on that account


# ------------------------------------------------------------------ config / cli
def test_config_rejects_duplicate_keys_and_percent_fees(tmp_path):
    from funding_capture.config import load_settings

    c = tmp_path / "c.yaml"
    c.write_text("portfolio:\n  capital_usd: 5000\nportfolio:\n  max_positions: 1\n")
    with pytest.raises(ValueError, match="duplicate key 'portfolio'"):
        load_settings(c)
    c.write_text("fees:\n  kraken:\n    spot_maker: 0.4000%\n")
    with pytest.raises(ValueError, match="not a number"):
        load_settings(c)
    c.write_text("fees:\n  kraken:\n    spot_maker: 0.4\n")
    with pytest.raises(ValueError, match="fractions"):
        load_settings(c)


def test_check_reports_missing_pem_per_venue(tmp_path, monkeypatch, capsys):
    from funding_capture.cli import main

    cfg = tmp_path / "c.yaml"
    cfg.write_text("venues:\n  coinbase: {kinds: [perp]}\n")
    monkeypatch.setenv("COINBASE_API_KEY", "organizations/x/apiKeys/y")
    monkeypatch.setenv("COINBASE_SECRET_FILE", str(tmp_path / "missing.pem"))
    with pytest.raises(SystemExit):
        main(["-c", str(cfg), "--env-file", str(tmp_path / "none"), "check"])
    out = capsys.readouterr().out
    assert "FAIL  credentials: COINBASE_SECRET_FILE: cannot read" in out


def test_live_refuses_old_state_layout(tmp_path):
    from funding_capture.cli import _engine
    from funding_capture.config import load_settings

    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"venues: {{}}\nportfolio:\n  state_dir: {tmp_path / 'state'}\n")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "engine.json").write_text("{}")
    with pytest.raises(SystemExit, match="older version"):
        _engine(load_settings(cfg), live=True)
