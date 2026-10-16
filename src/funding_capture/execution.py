"""Two-leg execution with leg-risk control.

The dangerous moment in a delta-neutral trade is between the two fills. The executor:

1. sends the leg with *thinner* displayed liquidity first, as an IOC limit a few bps
   through the touch, in slices sized off the touch and ``max_slice_usd``;
2. immediately hedges whatever filled on the other leg with IOC orders whose limit
   widens on each retry;
3. if the hedge still cannot be completed, unwinds the unhedged remainder of the
   first leg, and reports any residual it could not remove so the risk monitor can
   escalate.

It never leaves a slice's exposure open while moving on to the next slice.

Maker mode: for legs on ``maker_venues`` (venues with expensive taker fees, e.g. US spot
exchanges) the executor instead rests a post-only order at the touch, polls it, and hedges
each new partial fill on the other leg with IOC as soon as it sees it. The order is
re-priced when the market moves away and cancelled after ``maker_timeout_s``; whatever
filled stays hedged and the rest of the trade is simply not done. The engine is blocked
while this runs, so keep the timeout short.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .models import Candidate, Fill, Instrument, OrderState, Side
from .venues.base import Venue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionParams:
    max_slice_usd: float = 1_000.0
    touch_fraction: float = 0.5  # take at most this share of displayed touch size per slice
    entry_slippage_bps: float = 10.0  # limit offset through the touch for the first leg
    hedge_slippage_bps: float = 25.0  # initial offset for the hedge; grows each retry
    hedge_retries: int = 4
    max_first_leg_misses: int = 3
    residual_tolerance_usd: float = 5.0
    maker_timeout_s: float = 90.0  # per resting order before giving up on the rest
    maker_poll_s: float = 2.0
    maker_requote_bps: float = 5.0  # re-price when the touch moves this far from our order


@dataclass
class _LegPlan:
    inst: Instrument
    order_side: str
    reduce_only: bool


@dataclass
class _Seen:
    """What has already been recorded from one resting order."""

    qty: float = 0.0
    cost: float = 0.0
    fee: float = 0.0


@dataclass
class _Carry:
    """Maker fills too small to hedge yet (below the hedge venue's lot); hedged later."""

    qty: float = 0.0


@dataclass
class ExecutionReport:
    fills: list[Fill] = field(default_factory=list)
    hedged_qty: float = 0.0
    residual: dict[str, float] = field(default_factory=dict)  # instrument key -> signed qty
    complete: bool = False
    messages: list[str] = field(default_factory=list)
    # something went wrong in a way that leaves the true exposure uncertain (venue error,
    # an order that could not be confirmed cancelled): the engine halts entries on this
    unsafe: bool = False

    def _side(self, inst: Instrument, order_side: str) -> list[Fill]:
        return [f for f in self.fills if f.instrument == inst and f.order_side == order_side]

    def qty(self, inst: Instrument, order_side: str) -> float:
        return sum(f.qty for f in self._side(inst, order_side))

    def avg_price(self, inst: Instrument, order_side: str) -> float | None:
        fs = self._side(inst, order_side)
        q = sum(f.qty for f in fs)
        return sum(f.qty * f.price for f in fs) / q if q > 0 else None

    @property
    def fees_usd(self) -> float:
        return sum(f.fee_usd for f in self.fills)

    @property
    def has_residual(self) -> bool:
        return any(abs(v) > 0 for v in self.residual.values())


class PreTradeError(RuntimeError):
    """Market data failed before any order was sent: nothing was traded."""


class PairExecutor:
    def __init__(
        self,
        venues: dict[str, Venue],
        params: ExecutionParams | None = None,
        maker_venues: tuple[str, ...] = (),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.venues = venues
        self.p = params or ExecutionParams()
        self.maker_venues = set(maker_venues)
        self.sleep = sleep
        self.clock = clock

    # ------------------------------------------------------------------ public API
    def open(self, cand: Candidate, notional_usd: float) -> ExecutionReport:
        legs = [
            _LegPlan(cand.long, Side.LONG.order_side, False),
            _LegPlan(cand.short, Side.SHORT.order_side, False),
        ]
        try:
            quotes = self._quotes(legs)
        except Exception as exc:
            raise PreTradeError(f"no quotes: {exc}") from exc
        mid = (quotes[0].mid + quotes[1].mid) / 2
        return self._execute(legs, notional_usd / mid)

    def close(self, cand: Candidate, qty: float, urgent: bool = False) -> ExecutionReport:
        """Close both legs. ``urgent`` (risk exits) always takes liquidity, never rests."""
        legs = [
            _LegPlan(cand.long, Side.LONG.opposite.order_side, cand.long.is_perp),
            _LegPlan(cand.short, Side.SHORT.opposite.order_side, cand.short.is_perp),
        ]
        return self._execute(legs, qty, allow_maker=not urgent)

    def trade_one(self, inst: Instrument, order_side: str, qty: float, reduce_only: bool) -> Fill:
        """Single IOC on one leg (used by the rebalancer)."""
        q = self.venues[inst.venue].fetch_touch(inst)
        price, _ = q.touch(order_side)
        return self._ioc(
            _LegPlan(inst, order_side, reduce_only), qty, price, self.p.hedge_slippage_bps
        )

    # ------------------------------------------------------------------ internals
    def _quotes(self, legs: list[_LegPlan]):
        return [self.venues[leg.inst.venue].fetch_touch(leg.inst) for leg in legs]

    def _min_qty(self, a: _LegPlan, b: _LegPlan, price: float) -> float:
        """Smallest slice both venues accept, incl. min order value (10% buffer)."""
        va, vb = self.venues[a.inst.venue], self.venues[b.inst.venue]
        min_value = max(va.min_notional(a.inst), vb.min_notional(b.inst)) * 1.1
        return max(va.min_qty(a.inst), vb.min_qty(b.inst), min_value / price)

    def _sendable(self, leg: _LegPlan, qty: float, price: float) -> float:
        """``qty`` rounded to the venue's lot, or 0 if the venue would reject the order
        (below its minimum size or minimum order value, with a 10% buffer)."""
        v = self.venues[leg.inst.venue]
        q = v.round_qty(leg.inst, qty)
        if q <= 0 or q < v.min_qty(leg.inst) or q * price < v.min_notional(leg.inst) * 1.1:
            return 0.0
        return q

    def _round_both(self, a: _LegPlan, b: _LegPlan, qty: float) -> float:
        va, vb = self.venues[a.inst.venue], self.venues[b.inst.venue]
        q = va.round_qty(a.inst, qty)
        q = vb.round_qty(b.inst, q)
        return va.round_qty(a.inst, q)

    def _ioc(self, leg: _LegPlan, qty: float, touch: float, slip_bps: float) -> Fill:
        sign = 1 if leg.order_side == "buy" else -1
        limit = touch * (1 + sign * slip_bps / 1e4)
        return self.venues[leg.inst.venue].place_ioc(
            leg.inst, leg.order_side, qty, limit, reduce_only=leg.reduce_only
        )

    def _maker_leg(self, legs: list[_LegPlan]) -> int | None:
        """Index of the leg to work passively, if any (the spot leg if both qualify)."""
        idx = [
            i
            for i, leg in enumerate(legs)
            if leg.inst.venue in self.maker_venues
            and getattr(self.venues[leg.inst.venue], "supports_post_only", False)
        ]
        if not idx:
            return None
        if len(idx) == 1:
            return idx[0]
        return 0 if legs[0].inst.kind == "spot" else 1

    def _hedge(self, b: _LegPlan, qty: float, rep: ExecutionReport) -> float:
        """IOC ``qty`` on leg ``b`` with widening limits. Returns what is still unhedged,
        including any remainder too small for the venue to accept."""
        left = qty
        for attempt in range(self.p.hedge_retries):
            if left <= 1e-12:
                break
            touch_b, _ = self._quotes([b])[0].touch(b.order_side)
            lot = self._sendable(b, left, touch_b)
            if lot <= 0:
                break
            fb = self._ioc(b, lot, touch_b, self.p.hedge_slippage_bps * (attempt + 1))
            if fb.qty > 0:
                rep.fills.append(fb)
                left -= fb.qty
        return max(left, 0.0)

    def _finish(
        self,
        legs: list[_LegPlan],
        rep: ExecutionReport,
        target_qty: float,
        price: float,
        reason: str,
    ) -> ExecutionReport:
        """Flatten any one-sided excess (only ever called with nothing resting), then
        record what is left and whether the execution did what was asked."""
        try:
            self._unwind_excess(legs, rep, price, reason)
        except Exception as exc:  # the report must still reach the caller
            rep.unsafe = True
            rep.messages.append(f"unwind error: {exc}")
            log.error("unwind error: %s", exc)
        self._record_residual(legs, rep, price)
        rep.complete = (
            not rep.has_residual
            and not rep.unsafe
            and abs(target_qty - rep.hedged_qty) * price <= self.p.residual_tolerance_usd
        )
        return rep

    def _execute(
        self, legs: list[_LegPlan], target_qty: float, allow_maker: bool = True
    ) -> ExecutionReport:
        m = self._maker_leg(legs) if allow_maker else None
        if m is not None:
            return self._execute_maker(legs[m], legs[1 - m], legs, target_qty)
        rep = ExecutionReport()
        try:
            quotes = self._quotes(legs)
        except Exception as exc:
            raise PreTradeError(f"no quotes: {exc}") from exc
        price = quotes[0].mid
        # thinner touch (in USD) goes first: it's the leg most likely to under-fill
        depth = [q.touch(leg.order_side)[1] * q.mid for leg, q in zip(legs, quotes, strict=True)]
        first, second = (0, 1) if depth[0] <= depth[1] else (1, 0)
        a, b = legs[first], legs[second]
        min_qty = self._min_qty(a, b, quotes[first].mid)
        reason = "sub-lot remainder"
        remaining = target_qty
        misses = 0
        carry = 0.0  # first-leg fills too small to hedge yet
        try:
            while remaining > 0:
                qa = self._quotes([a])[0]
                touch_a, size_a = qa.touch(a.order_side)
                slice_qty = min(  # caps never below one sendable order: whole contracts
                    remaining,
                    max(self.p.max_slice_usd / qa.mid, min_qty),
                    max(size_a * self.p.touch_fraction, min_qty),  # the IOC limit bounds price
                )
                slice_qty = self._round_both(a, b, slice_qty)
                if slice_qty <= 0 or slice_qty < min_qty:
                    if remaining * qa.mid > self.p.residual_tolerance_usd:
                        rep.messages.append(f"stopped: {remaining:.6g} left below lot/min size")
                    break
                fa = self._ioc(a, slice_qty, touch_a, self.p.entry_slippage_bps)
                if fa.qty <= 0:
                    misses += 1
                    if misses >= self.p.max_first_leg_misses:
                        rep.messages.append(f"first leg {a.inst.key} not filling; aborted")
                        break
                    continue
                rep.fills.append(fa)
                remaining -= fa.qty
                want = fa.qty + carry
                left = self._hedge(b, want, rep)
                rep.hedged_qty += want - left
                carry = left
                if self._sendable(b, left, qa.mid) > 0:
                    reason = "hedge failed"  # the hedge venue refused a sendable amount
                    break
            if reason == "sub-lot remainder" and carry > 1e-12:  # fills below one hedge lot
                c = _Carry(carry)
                remaining -= self._top_up(a, b, c, rep, remaining)
                carry = c.qty
        except Exception as exc:
            rep.unsafe = True
            reason = "execution error"
            rep.messages.append(f"execution error: {exc}")
            log.error("execution error on %s/%s: %s", a.inst.key, b.inst.key, exc)
        return self._finish(legs, rep, target_qty, price, reason)

    def _execute_maker(
        self, m: _LegPlan, h: _LegPlan, legs: list[_LegPlan], target_qty: float
    ) -> ExecutionReport:
        rep = ExecutionReport()
        vm = self.venues[m.inst.venue]
        try:  # both legs: a hedge leg with no book (halted) must stop us before any order
            ref_mid = self._quotes([m, h])[0].mid
        except Exception as exc:
            raise PreTradeError(f"no quotes: {exc}") from exc
        min_qty = self._min_qty(m, h, ref_mid)
        remaining = target_qty
        rejects = 0
        carry = _Carry()
        reason = "sub-lot remainder"
        failed = timed_out = False
        started = self.clock()  # one budget across re-quotes; reset only on progress
        try:
            while remaining > 0 and not (failed or timed_out):
                qm = self._quotes([m])[0]
                price = qm.bid if m.order_side == "buy" else qm.ask  # join our side
                cap = max(self.p.max_slice_usd / qm.mid, min_qty)  # at least one sendable order
                slice_qty = self._round_both(m, h, min(remaining, cap))
                if slice_qty <= 0 or slice_qty < min_qty:
                    if remaining * qm.mid > self.p.residual_tolerance_usd:
                        rep.messages.append(f"stopped: {remaining:.6g} left below lot/min size")
                    break
                if self.clock() - started >= self.p.maker_timeout_s:
                    timed_out = True
                    break
                oid = vm.place_post_only(m.inst, m.order_side, slice_qty, price, m.reduce_only)
                if oid is None:  # the book moved and we would have crossed; re-read
                    rejects += 1
                    if rejects >= self.p.max_first_leg_misses:
                        rep.messages.append(f"post-only on {m.inst.key} kept crossing; aborted")
                        break
                    continue
                seen = _Seen()
                resting = True
                try:
                    while True:
                        self.sleep(self.p.maker_poll_s)
                        vm.heartbeat()  # keep a dead-man switch alive during long waits
                        st = vm.fetch_order_state(m.inst, oid)
                        resting = st.is_open
                        if not self._absorb(m, h, st, seen, carry, rep, hedge=True):
                            failed = True
                            reason = "hedge failed"
                            break
                        if not resting:
                            break
                        expired = self.clock() - started >= self.p.maker_timeout_s
                        if not expired and not self._moved_away(m, price):
                            continue
                        timed_out = expired  # otherwise: re-price at the new touch
                        break
                finally:
                    if resting:  # never leave our order on the book when we stop looking
                        final = vm.cancel_order(m.inst, oid)
                        if not self._absorb(m, h, final, seen, carry, rep, hedge=not failed):
                            failed, reason = True, "hedge failed"
                        if final.is_open:
                            rep.unsafe = True
                            rep.messages.append(f"order {oid} on {m.inst.key} may still be open")
                remaining -= seen.qty
                if seen.qty > 0:
                    started = self.clock()
            if not failed and carry.qty > 1e-12:
                remaining -= self._top_up(m, h, carry, rep, remaining)
            if timed_out:
                rep.messages.append(
                    f"maker order on {m.inst.key} timed out; {remaining:.6g} not traded"
                )
        except Exception as exc:
            rep.unsafe = True
            reason = "execution error"
            rep.messages.append(f"execution error: {exc}")
            log.error("maker execution error on %s: %s", m.inst.key, exc)
        return self._finish(legs, rep, target_qty, ref_mid, reason)

    def _absorb(
        self,
        m: _LegPlan,
        h: _LegPlan,
        st: OrderState,
        seen: _Seen,
        carry: _Carry,
        rep: ExecutionReport,
        hedge: bool = True,
    ) -> bool:
        """Record new maker fills and (if ``hedge``) hedge them plus any carried remainder.
        Returns False if the hedge venue refused a sendable amount; the unhedged part is
        left for ``_finish`` to unwind once our resting order has been cancelled."""
        new = st.filled - seen.qty
        if new > 1e-12:
            cost_now = st.filled * (st.avg_price or 0.0)
            price = (cost_now - seen.cost) / new
            rep.fills.append(
                Fill(m.inst, m.order_side, new, price, st.fee_usd - seen.fee, st.order_id)
            )
            seen.qty, seen.cost, seen.fee = st.filled, cost_now, st.fee_usd
            carry.qty += new
        if not hedge or carry.qty <= 1e-12:
            return True
        want = carry.qty
        left = self._hedge(h, want, rep)
        rep.hedged_qty += want - left
        carry.qty = left
        touch = self._quotes([h])[0].mid if left > 1e-12 else 0.0
        return not (left > 1e-12 and self._sendable(h, left, touch) > 0)

    def _top_up(
        self, m: _LegPlan, h: _LegPlan, carry: _Carry, rep: ExecutionReport, room: float
    ) -> float:
        """Maker fills left below one hedge lot (a part-filled order on a whole-contract
        perp): take the rest of that lot on the maker leg with an IOC and hedge it all,
        instead of selling the fills back and paying the fees twice. Returns the qty
        bought or sold. Never trades more than ``room``, what is still to be done."""
        lot = self.venues[h.inst.venue].min_qty(h.inst)
        if lot <= 0:
            return 0.0
        target = math.ceil(carry.qty / lot - 1e-9) * lot
        need = target - carry.qty
        vm = self.venues[m.inst.venue]
        q = vm.round_qty(m.inst, need + 1e-9 * max(1.0, need))  # onto the grid, not under
        if need <= 1e-12 or q < need * (1 - 1e-9) or q > room * (1 + 1e-9):
            return 0.0
        got = q - self._hedge(m, q, rep)  # IOCs with widening limits; skips sub-minimum sizes
        if got <= 1e-12:
            return 0.0
        carry.qty += got
        want = carry.qty
        left = self._hedge(h, want, rep)
        rep.hedged_qty += want - left
        carry.qty = left
        rep.messages.append(f"topped up {got:.6g} on {m.inst.key} to a whole hedge lot")
        return got

    def _moved_away(self, m: _LegPlan, our_price: float) -> bool:
        q = self._quotes([m])[0]
        best = q.bid if m.order_side == "buy" else q.ask
        drift = (best - our_price) if m.order_side == "buy" else (our_price - best)
        return drift / our_price * 1e4 > self.p.maker_requote_bps

    def _unwind_excess(
        self, legs: list[_LegPlan], rep: ExecutionReport, price: float, reason: str
    ) -> None:
        """Trade back whatever one leg did beyond the other, on the leg that did more."""
        done = []
        for leg in legs:
            fwd = sum(
                f.qty
                for f in rep.fills
                if f.instrument == leg.inst and f.order_side == leg.order_side
            )
            back = sum(
                f.qty
                for f in rep.fills
                if f.instrument == leg.inst and f.order_side != leg.order_side
            )
            done.append(fwd - back)
        big = 0 if done[0] >= done[1] else 1
        excess = done[big] - done[1 - big]
        if excess * price > self.p.residual_tolerance_usd:
            self._unwind(legs[big], excess, rep, reason)

    def _unwind(
        self, leg: _LegPlan, qty: float, rep: ExecutionReport, reason: str = "hedge failed"
    ) -> None:
        # undoing an opening order reduces the position; undoing a closing order re-opens it
        back = _LegPlan(
            leg.inst,
            "sell" if leg.order_side == "buy" else "buy",
            leg.inst.is_perp and not leg.reduce_only,
        )
        left = qty
        for attempt in range(self.p.hedge_retries):
            touch, _ = self._quotes([back])[0].touch(back.order_side)
            lot = self._sendable(back, left, touch)
            if lot <= 0:
                break
            f = self._ioc(back, lot, touch, self.p.hedge_slippage_bps * (attempt + 2))
            if f.qty > 0:
                rep.fills.append(f)
                left -= f.qty
        msg = f"{reason}: unwound {qty - left:.6g} of {leg.inst.key}"
        if left * qty > 0 and self.venues[leg.inst.venue].round_qty(leg.inst, left) > 0:
            msg += f", {left:.6g} STILL OPEN"
        log.warning(msg)
        rep.messages.append(msg)

    def _record_residual(self, legs: list[_LegPlan], rep: ExecutionReport, price: float) -> None:
        """Net signed exposure this execution left behind, in asset units."""
        net = 0.0
        for f in rep.fills:
            net += f.qty if f.order_side == "buy" else -f.qty
        if abs(net) * price > self.p.residual_tolerance_usd:
            # attribute to the first leg's instrument for reporting; both are the same asset
            rep.residual[legs[0].inst.key] = net
