import pytest
from conftest import HOUR, T0, FakeVenue, perp, spot

from funding_capture.costs import VenueFees
from funding_capture.execution import ExecutionParams, PairExecutor
from funding_capture.models import Candidate, Construction, PairPosition
from funding_capture.risk import Action, LegView, RiskLimits, check_position
from funding_capture.venues.paper import PaperVenue

FEES = VenueFees(spot_taker=0.001, perp_taker=0.0005)


def _paper(name, insts, **kw):
    data = FakeVenue(name, insts)
    return data, PaperVenue(data, FEES, **kw)


# ----------------------------------------------------------------------------- paper venue
def test_paper_fills_at_touch_respects_limit_and_reduce_only():
    p = perp("a")
    data, v = _paper("a", [p])
    v.load_instruments()
    data.set_quote(p, 99, 101, size=5)
    assert v.place_ioc(p, "buy", 1, limit_price=100).qty == 0  # limit below ask
    f = v.place_ioc(p, "buy", 10, limit_price=102)
    assert f.qty == 5 and f.price == 101  # capped by touch size
    assert f.fee_usd == pytest.approx(5 * 101 * 0.0005)
    assert v.place_ioc(p, "sell", 8, 90, reduce_only=True).qty == 5  # can't flip with reduce
    assert v.fetch_perp_positions() == {}


def test_paper_settles_funding_on_tick():
    p = perp("a")
    data, v = _paper("a", [p])
    v.load_instruments()
    data.set_quote(p, 100, 100)
    data.set_funding(p, 0.001, next_ts=T0 + HOUR, mark=100)
    v.place_ioc(p, "sell", 2, 99)  # short 2 @ 100
    v.tick(T0)  # records the pending settlement
    v.tick(T0 + HOUR - 1)
    assert v.fetch_funding_payments(p, 0) == []
    data.set_funding(p, 0.002, next_ts=T0 + 2 * HOUR, mark=100)
    v.tick(T0 + HOUR)
    # short receives the rate seen before the settlement: 0.001 * 2 * 100
    assert v.fetch_funding_payments(p, 0) == [(T0 + HOUR, pytest.approx(0.2))]
    pos = v.fetch_perp_positions()[p.key]
    assert pos.qty == -2 and pos.liquidation_price == pytest.approx(100 * (1 + 0.5 - 0.01))


# ----------------------------------------------------------------------------- executor
def _pair(spread_ok=True):
    s, p = spot("a"), perp("b")
    da, va = _paper("a", [s])
    db, vb = _paper("b", [p])
    va.load_instruments()
    vb.load_instruments()
    da.set_quote(s, 99.9, 100.1, size=1_000)
    db.set_quote(p, 100.0, 100.2, size=1_000)
    cand = Candidate(Construction.SPOT_PERP, "BTC", long=s, short=p)
    ex = PairExecutor({"a": va, "b": vb}, ExecutionParams(max_slice_usd=250))
    return cand, ex, (da, va), (db, vb)


def test_open_and_close_fully_hedged_in_slices():
    cand, ex, (da, va), (db, vb) = _pair()
    rep = ex.open(cand, 1_000)
    assert rep.complete and not rep.has_residual
    assert rep.hedged_qty == pytest.approx(1_000 / 100.05, abs=0.002)
    assert len([f for f in rep.fills if f.instrument == cand.long]) >= 4  # $250 slices
    assert va.fetch_spot_balance(cand.long) == pytest.approx(-vb.state.books[cand.short.key].qty)
    close = ex.close(cand, rep.hedged_qty)
    assert close.complete
    assert va.fetch_spot_balance(cand.long) == pytest.approx(0, abs=1e-9)
    assert vb.fetch_perp_positions() == {}


def test_failed_hedge_unwinds_first_leg():
    cand, ex, (da, va), (db, vb) = _pair()
    # perp leg has displayed size but its bid collapses: hedge sells never cross
    db.set_quote(cand.short, 50.0, 100.2, size=1_000)
    da.set_quote(cand.long, 99.9, 100.1, size=1)  # spot is thinner -> goes first

    orig = vb.place_ioc

    def no_fill(inst, side, qty, limit, reduce_only=False):
        return orig(inst, side, qty, 1e9 if side == "sell" else limit, reduce_only)

    vb.place_ioc = no_fill
    rep = ex.open(cand, 1_000)
    assert not rep.complete
    assert rep.hedged_qty == 0
    assert va.fetch_spot_balance(cand.long) == pytest.approx(0, abs=1e-9)  # unwound
    assert not rep.has_residual
    assert any("hedge failed" in m for m in rep.messages)
    assert len(rep.fills) == 2  # one buy, one unwind: it stops instead of retrying slices


def test_first_leg_that_never_fills_aborts_cleanly():
    cand, ex, (da, va), (db, vb) = _pair()
    da.set_quote(cand.long, 99.9, 100.1, size=0.0)  # nothing on the ask
    rep = ex.open(cand, 1_000)
    assert not rep.complete and rep.fills == []


# ----------------------------------------------------------------------------- risk
def _pos(qty=1.0):
    cand = Candidate(Construction.SPOT_PERP, "BTC", long=spot("a"), short=perp("b"))
    return PairPosition("P1", cand, qty, 100.0, 100.0, T0)


def test_risk_checks():
    lim = RiskLimits()
    pos = _pos()
    healthy = check_position(pos, LegView(1.0, 100), LegView(-1.0, 100, 160), lim)
    assert healthy == []
    # extra spot coins in the wallet are not drift
    assert check_position(pos, LegView(5.0, 100), LegView(-1.0, 100, 160), lim) == []
    drift = check_position(pos, LegView(1.0, 100), LegView(-0.95, 100, 160), lim)
    assert [a.action for a in drift] == [Action.REBALANCE]
    adl = check_position(pos, LegView(1.0, 100), LegView(-0.5, 100, 160), lim)
    assert Action.CLOSE in [a.action for a in adl]
    near_liq = check_position(pos, LegView(1.0, 100), LegView(-1.0, 100, 110), lim)
    assert [a.action for a in near_liq] == [Action.CLOSE]
    warn_liq = check_position(pos, LegView(1.0, 100), LegView(-1.0, 100, 130), lim)
    assert [a.action for a in warn_liq] == [Action.WARN]
    diverged = check_position(pos, LegView(1.0, 100), LegView(-1.0, 105, 200), lim)
    assert Action.CLOSE in [a.action for a in diverged]


def _whole_contract_pair(**params):
    """Spot on a, a perp on b traded in whole contracts of 10 units ($1,000 each)."""
    s, p = spot("a"), perp("b")
    da, va = _paper("a", [s])
    db, vb = _paper("b", [p], lot_step=10)
    va.load_instruments()
    vb.load_instruments()
    da.set_quote(s, 99.9, 100.1, size=1_000)
    db.set_quote(p, 100.0, 100.2, size=1_000)
    vb.min_qty = lambda inst: 10.0
    cand = Candidate(Construction.SPOT_PERP, "BTC", long=s, short=p)
    return cand, (da, va), (db, vb)


def test_contract_worth_more_than_a_slice_still_closes():
    cand, (da, va), (db, vb) = _whole_contract_pair()
    ex = PairExecutor({"a": va, "b": vb}, ExecutionParams(max_slice_usd=250))
    assert ex.open(cand, 1_010).hedged_qty == pytest.approx(10)
    close = ex.close(cand, 10, urgent=True)
    assert close.complete and vb.fetch_perp_positions() == {}
    assert va.fetch_spot_balance(cand.long) == pytest.approx(0, abs=1e-9)


def test_maker_fill_below_one_contract_is_topped_up_not_sold_back():
    from funding_capture.models import OrderState

    cand, (da, va), (db, vb) = _whole_contract_pair()
    t = [0.0]
    ex = PairExecutor(
        {"a": va, "b": vb},
        ExecutionParams(max_slice_usd=5_000, maker_timeout_s=60, maker_poll_s=10),
        maker_venues=("a",),
        sleep=lambda sec: t.__setitem__(0, t[0] + sec),
        clock=lambda: t[0],
    )
    done = {}

    def part_filled(inst, oid):  # 4 of the 10 fill, then the order just rests
        if oid not in done:
            done[oid] = True
            va._apply(va._book(inst), 4.0, 99.9)
        return OrderState(oid, 4.0, 99.9, 0.4, True)

    va.fetch_order_state = part_filled
    va.cancel_order = lambda inst, oid: OrderState(oid, 4.0, 99.9, 0.4, False)
    reads, real_touch = [], vb.fetch_touch
    vb.fetch_touch = lambda inst: reads.append(1) or real_touch(inst)
    rep = ex.open(cand, 1_010)
    assert rep.hedged_qty == pytest.approx(10)  # 4 resting + 6 taken, one contract hedged
    assert len(reads) < 6  # no hedge-leg book reads on every poll while nothing new fills
    assert not any(m.startswith(("stopped:", "maker order")) for m in rep.messages)
    assert va.fetch_spot_balance(cand.long) == pytest.approx(10)
    assert vb.fetch_perp_positions()[cand.short.key].qty == pytest.approx(-10)
    assert not [f for f in rep.fills if f.instrument == cand.long and f.order_side == "sell"]
    assert any("topped up" in m for m in rep.messages)


def test_ioc_close_with_a_thin_spot_touch_tops_up_instead_of_selling_back():
    cand, (da, va), (db, vb) = _whole_contract_pair()
    ex = PairExecutor({"a": va, "b": vb}, ExecutionParams(max_slice_usd=1_000))
    assert ex.open(cand, 1_010).hedged_qty == pytest.approx(10)
    da.set_quote(cand.long, 99.9, 100.1, size=3)  # less than one contract on the bid
    db.set_quote(cand.short, 100.0, 100.2, size=12)
    rep = ex.close(cand, 10, urgent=True)
    assert rep.complete and vb.fetch_perp_positions() == {}
    assert va.fetch_spot_balance(cand.long) == pytest.approx(0, abs=1e-9)
    assert not [f for f in rep.fills if f.instrument == cand.long and f.order_side == "buy"]


def test_top_up_below_the_maker_venue_minimum_is_not_an_error():
    from funding_capture.models import OrderState

    cand, (da, va), (db, vb) = _whole_contract_pair()
    va.min_qty = lambda inst: 0.5  # Kraken-style minimum order on the spot leg
    real_ioc = va.place_ioc

    def kraken_ioc(inst, side, qty, limit, reduce_only=False):
        if qty < 0.5:
            raise RuntimeError('kraken {"error":["EOrder:Order minimum not met"]}')
        return real_ioc(inst, side, qty, limit, reduce_only)

    va.place_ioc = kraken_ioc
    t = [0.0]
    ex = PairExecutor(
        {"a": va, "b": vb},
        ExecutionParams(max_slice_usd=5_000, maker_timeout_s=60, maker_poll_s=10),
        maker_venues=("a",),
        sleep=lambda sec: t.__setitem__(0, t[0] + sec),
        clock=lambda: t[0],
    )
    done = {}

    def part_filled(inst, oid):  # 9.8 of 10 fill: the 0.2 left is below the minimum
        if oid not in done:
            done[oid] = True
            va._apply(va._book(inst), 9.8, 99.9)
        return OrderState(oid, 9.8, 99.9, 1.0, True)

    va.fetch_order_state = part_filled
    va.cancel_order = lambda inst, oid: OrderState(oid, 9.8, 99.9, 1.0, False)
    rep = ex.open(cand, 1_010)
    assert not rep.unsafe and not rep.has_residual  # unwound, but no "execution error"


def test_float_noise_just_under_a_contract_still_closes():
    cand, (da, va), (db, vb) = _whole_contract_pair()
    ex = PairExecutor({"a": va, "b": vb}, ExecutionParams(max_slice_usd=1_000))
    assert ex.open(cand, 1_010).hedged_qty == pytest.approx(10)
    import math

    # exchanges truncate strictly (Kraken: 8 decimals), unlike the paper venue's floor
    va.round_qty = lambda inst, q: math.floor(q * 1e8) / 1e8
    vb.round_qty = lambda inst, q: math.floor(q / 10) * 10.0
    qty = 9.999999999999998  # a sum of fills a hair under one contract
    assert qty < 10
    rep = ex.close(cand, qty, urgent=True)
    assert rep.complete and vb.fetch_perp_positions() == {}


def test_whole_lot_refused_after_a_top_up_is_reported_as_a_failed_hedge():
    cand, (da, va), (db, vb) = _whole_contract_pair()
    ex = PairExecutor({"a": va, "b": vb}, ExecutionParams(max_slice_usd=1_000))
    ex.open(cand, 1_010)
    da.set_quote(cand.long, 99.9, 100.1, size=3)
    real = vb.place_ioc
    vb.place_ioc = lambda inst, side, qty, limit, reduce_only=False: real(
        inst,
        side,
        qty,
        1.0 if side == "buy" else limit,
        reduce_only,  # buys never fill
    )
    rep = ex.close(cand, 10, urgent=True)
    assert any(m.startswith("hedge failed") for m in rep.messages)
    assert va.fetch_spot_balance(cand.long) == pytest.approx(10)  # sold back, still hedged


def test_paper_venue_keeps_positions_on_products_held_back_as_halted():
    p = perp("a")
    data = FakeVenue("a", [])
    data._untradeable = {p.key: p}
    v = PaperVenue(data, FEES)
    data.set_quote(p, 100, 100.2)
    v.load_instruments()
    v._apply(v._book(p), -2.0, 100.0)
    assert v.fetch_perp_positions()[p.key].qty == -2
