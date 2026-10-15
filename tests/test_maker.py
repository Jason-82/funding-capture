"""Maker (post-only) execution: rest on the expensive spot leg, hedge each fill with IOC."""

import pytest
from conftest import FakeVenue, perp, spot

from funding_capture.costs import CostModel, VenueFees
from funding_capture.execution import ExecutionParams, PairExecutor
from funding_capture.models import Candidate, Construction
from funding_capture.venues.paper import PaperVenue

SPOT_FEES = VenueFees(spot_taker=0.004, spot_maker=0.0025)
PERP_FEES = VenueFees(perp_taker=0.00045, perp_maker=0.00015)


class Sim:
    """Spot on 'kraken' (maker venue), perp on 'hl'; a scripted market moves on each poll."""

    def __init__(self, script=None, **params):
        self.s, self.p = spot("kraken", quote="USD"), perp("hl", quote="USDC")
        self.dk, self.dh = FakeVenue("kraken", [self.s]), FakeVenue("hl", [self.p])
        self.dk.set_quote(self.s, 99.9, 100.1, size=1e6)
        self.dh.set_quote(self.p, 100.0, 100.05, size=1e6)
        self.vk, self.vh = PaperVenue(self.dk, SPOT_FEES), PaperVenue(self.dh, PERP_FEES)
        self.vk.load_instruments()
        self.vh.load_instruments()
        self.t = 0.0
        self.polls = 0
        self.script = script or {}
        self.ex = PairExecutor(
            {"kraken": self.vk, "hl": self.vh},
            ExecutionParams(maker_timeout_s=30, maker_poll_s=2, **params),
            maker_venues=("kraken",),
            sleep=self.sleep,
            clock=lambda: self.t,
        )
        self.cand = Candidate(Construction.SPOT_PERP, "BTC", long=self.s, short=self.p)

    def sleep(self, dt):
        self.t += dt
        self.polls += 1
        if self.polls in self.script:
            bid, ask = self.script[self.polls]
            self.dk.set_quote(self.s, bid, ask, size=1e6)


def test_rests_at_bid_then_hedges_when_traded_through():
    sim = Sim(script={2: (99.7, 99.9)})  # second poll: sellers take out our 99.9 bid
    rep = sim.ex.open(sim.cand, 900)
    assert rep.complete and not rep.has_residual
    spot_fills = [f for f in rep.fills if f.instrument == sim.s]
    assert all(f.price == 99.9 for f in spot_fills)  # filled at our resting price
    assert spot_fills[0].fee_usd == pytest.approx(spot_fills[0].notional * 0.0025)  # maker fee
    assert sim.vk.fetch_spot_balance(sim.s) == pytest.approx(-sim.vh.state.books[sim.p.key].qty)
    assert rep.hedged_qty == pytest.approx(rep.qty(sim.s, "buy"))


def test_requotes_when_market_moves_away():
    # poll 1: bid lifts 20 bps -> cancel and re-rest at 100.1; poll 3: traded through
    sim = Sim(script={1: (100.1, 100.3), 3: (99.9, 100.1)})
    rep = sim.ex.open(sim.cand, 900)
    assert rep.complete
    assert {f.price for f in rep.fills if f.instrument == sim.s} == {100.1}


def test_times_out_without_leaving_exposure():
    sim = Sim()  # market never comes to us
    rep = sim.ex.open(sim.cand, 900)
    assert not rep.complete and rep.fills == [] and not rep.has_residual
    assert any("timed out" in m for m in rep.messages)
    assert sim.t >= 30


def test_unwinds_maker_fill_when_hedge_fails():
    sim = Sim(script={1: (99.7, 99.9)})
    orig = sim.vh.place_ioc
    sim.vh.place_ioc = lambda inst, side, qty, limit, reduce_only=False: orig(
        inst,
        side,
        qty,
        1e9,
        reduce_only,  # a sell limit nobody will hit
    )
    rep = sim.ex.open(sim.cand, 900)
    assert not rep.complete and not rep.has_residual
    assert sim.vk.fetch_spot_balance(sim.s) == pytest.approx(0.0, abs=1e-9)
    assert any("hedge failed" in m for m in rep.messages)


def test_close_works_passively_too():
    sim = Sim(script={1: (99.7, 99.9)})
    opened = sim.ex.open(sim.cand, 900)
    # now sell the spot passively at the ask; buyers lift through it on the next poll
    sim.script = {sim.polls + 1: (100.1, 100.3)}
    closed = sim.ex.close(sim.cand, opened.hedged_qty)
    assert closed.complete
    assert sim.vk.fetch_spot_balance(sim.s) == pytest.approx(0.0, abs=1e-9)
    assert sim.vh.fetch_perp_positions() == {}


def test_cost_model_uses_maker_fee_on_maker_venues():
    cand = Candidate(Construction.SPOT_PERP, "BTC", long=spot("kraken"), short=perp("hl"))
    fees = {"kraken": SPOT_FEES, "hl": PERP_FEES}
    taker = CostModel(fees=fees, slippage_bps=3)
    maker = CostModel(fees=fees, slippage_bps=3, maker_venues=("kraken",))
    assert taker.round_trip_cost(cand) == pytest.approx(2 * (0.004 + 0.0003 + 0.00045 + 0.0003))
    assert maker.round_trip_cost(cand) == pytest.approx(2 * (0.0025 + 0.00045 + 0.0003))


def test_urgent_close_takes_liquidity():
    sim = Sim(script={1: (99.7, 99.9)})
    opened = sim.ex.open(sim.cand, 900)
    polls = sim.polls
    closed = sim.ex.close(sim.cand, opened.hedged_qty, urgent=True)
    assert closed.complete and sim.polls == polls  # no resting, no waiting
    spot_sells = [f for f in closed.fills if f.instrument == sim.s]
    assert spot_sells[0].fee_usd == pytest.approx(spot_sells[0].notional * 0.004)  # taker


class PartialMaker(PaperVenue):
    """A maker venue whose resting order fills in scripted cumulative steps."""

    def __init__(self, data, fees, steps):
        super().__init__(data, fees)
        self.steps, self.i, self.price, self.side = list(steps), 0, None, None

    def place_post_only(self, inst, order_side, qty, price, reduce_only=False):
        self.price, self.side, self.inst = price, order_side, inst
        return "po-1"

    def _state(self, is_open):
        from funding_capture.models import OrderState

        filled = self.steps[min(self.i, len(self.steps)) - 1] if self.i else 0.0
        return OrderState("po-1", filled, self.price, 0.0, is_open)

    def fetch_order_state(self, inst, order_id):
        prev = self._state(True).filled
        self.i += 1
        st = self._state(self.i < len(self.steps))
        self._apply(self._book(inst), st.filled - prev, self.price)
        return st

    def cancel_order(self, inst, order_id):
        return self._state(False)


def test_partial_maker_fills_below_hedge_lot_are_carried_not_lost():
    s, p = spot("kraken", quote="USD"), perp("hl", quote="USDC")
    dk, dh = FakeVenue("kraken", [s]), FakeVenue("hl", [p])
    dk.set_quote(s, 99.9, 100.1)
    dh.set_quote(p, 100.0, 100.05)
    vk = PartialMaker(dk, SPOT_FEES, steps=[0.3, 0.6, 1.0])
    vh = PaperVenue(dh, PERP_FEES, lot_step=0.5)  # hedge venue trades in 0.5 lots
    vk.load_instruments()
    vh.load_instruments()
    ex = PairExecutor(
        {"kraken": vk, "hl": vh}, ExecutionParams(), ("kraken",), sleep=lambda _: None
    )
    rep = ex.open(Candidate(Construction.SPOT_PERP, "BTC", long=s, short=p), 100.0)
    perp_sells = [f.qty for f in rep.fills if f.instrument == p]
    assert perp_sells == [0.5, 0.5]  # nothing after 0.3, one lot after 0.6, one after 1.0
    assert rep.hedged_qty == pytest.approx(1.0)
    assert rep.complete and not rep.has_residual
    assert vh.state.books[p.key].qty == pytest.approx(-1.0)


def test_cost_model_works_only_one_leg_passively():
    both = Candidate(Construction.SPOT_PERP, "BTC", long=spot("coinbase"), short=perp("coinbase"))
    fees = {"coinbase": VenueFees(0.009, 0.005, 0.0003, 0.0002)}
    costs = CostModel(fees=fees, slippage_bps=0, maker_venues=("coinbase",))
    assert costs.maker_legs(both) == (True, False)  # spot rests, perp takes
    assert costs.round_trip_cost(both) == pytest.approx(2 * (0.005 + 0.0003))
