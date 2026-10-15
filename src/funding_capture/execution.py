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
        quotes = self._quotes(legs)
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
        """Index of the leg to work passively, if any (the pricier one if both qualify)."""
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
        """IOC ``qty`` on leg ``b`` with widening limits.

        Returns what is still unhedged, *including* any remainder smaller than ``b``'s lot
        size (callers tell the two apart with ``round_qty``)."""
        vb = self.venues[b.inst.venue]
        left = qty
        for attempt in range(self.p.hedge_retries):
            lot = vb.round_qty(b.inst, left)
            if lot <= 0:
                break
            touch_b, _ = self._quotes([b])[0].touch(b.order_side)
            fb = self._ioc(b, lot, touch_b, self.p.hedge_slippage_bps * (attempt + 1))
            if fb.qty > 0:
                rep.fills.append(fb)
                left -= fb.qty
        return max(left, 0.0)

    def _execute(
        self, legs: list[_LegPlan], target_qty: float, allow_maker: bool = True
    ) -> ExecutionReport:
        m = self._maker_leg(legs) if allow_maker else None
        if m is not None:
            return self._execute_maker(legs[m], legs[1 - m], legs, target_qty)
        rep = ExecutionReport()
        quotes = self._quotes(legs)
        # thinner touch (in USD) goes first: it's the leg most likely to under-fill
        depth = [q.touch(leg.order_side)[1] * q.mid for leg, q in zip(legs, quotes, strict=True)]
        first, second = (0, 1) if depth[0] <= depth[1] else (1, 0)
        a, b = legs[first], legs[second]
        vb = self.venues[b.inst.venue]
        min_qty = self._min_qty(a, b, quotes[first].mid)

        remaining = target_qty
        misses = 0
        while remaining > 0:
            qa, qb = self._quotes([a, b])
            touch_a, size_a = qa.touch(a.order_side)
            slice_qty = min(
                remaining,
                self.p.max_slice_usd / qa.mid,
                size_a * self.p.touch_fraction,
            )
            slice_qty = self._round_both(a, b, slice_qty)
            if slice_qty <= 0 or slice_qty < min_qty:
                if remaining * qa.mid > self.p.residual_tolerance_usd:
                    rep.messages.append(f"stopped: {remaining:.6g} left below lot/min size")
                break

            fa = self._ioc(a, slice_qty, touch_a, self.p.entry_slippage_bps)
            if fa.qty > 0:
                rep.fills.append(fa)
            else:
                misses += 1
                if misses >= self.p.max_first_leg_misses:
                    rep.messages.append(f"first leg {a.inst.key} not filling; aborted")
                    break
                continue

            need = self._hedge(b, fa.qty, rep)
            rep.hedged_qty += fa.qty - need
            remaining -= fa.qty
            if need * qb.mid > self.p.residual_tolerance_usd:
                failed = vb.round_qty(b.inst, need) > 0
                self._unwind(a, need, rep, "hedge failed" if failed else "sub-lot remainder")
                if failed:
                    break  # the hedge venue is not cooperating; don't keep paying to retry

        self._record_residual(legs, rep, quotes[0].mid)
        rep.complete = (
            not rep.has_residual
            and abs(target_qty - rep.hedged_qty) * quotes[0].mid <= self.p.residual_tolerance_usd
        )
        return rep

    def _execute_maker(
        self, m: _LegPlan, h: _LegPlan, legs: list[_LegPlan], target_qty: float
    ) -> ExecutionReport:
        rep = ExecutionReport()
        vm = self.venues[m.inst.venue]
        ref_mid = self._quotes([m])[0].mid
        min_qty = self._min_qty(m, h, ref_mid)
        remaining = target_qty
        rejects = 0
        carry = _Carry()
        failed = timed_out = False
        while remaining > 0 and not (failed or timed_out):
            qm = self._quotes([m])[0]
            price = qm.bid if m.order_side == "buy" else qm.ask  # join our side of the book
            slice_qty = self._round_both(m, h, min(remaining, self.p.max_slice_usd / qm.mid))
            if slice_qty <= 0 or slice_qty < min_qty:
                if remaining * qm.mid > self.p.residual_tolerance_usd:
                    rep.messages.append(f"stopped: {remaining:.6g} left below lot/min size")
                break
            oid = vm.place_post_only(m.inst, m.order_side, slice_qty, price, m.reduce_only)
            if oid is None:  # the book moved and we would have crossed; re-read and retry
                rejects += 1
                if rejects >= self.p.max_first_leg_misses:
                    rep.messages.append(f"post-only on {m.inst.key} kept crossing; aborted")
                    break
                continue
            seen = _Seen()
            started = self.clock()
            while True:
                self.sleep(self.p.maker_poll_s)
                st = vm.fetch_order_state(m.inst, oid)
                if not self._absorb(m, h, st, seen, carry, rep):
                    self._absorb(m, h, vm.cancel_order(m.inst, oid), seen, carry, rep)
                    failed = True
                    break
                if not st.is_open:
                    break
                expired = self.clock() - started >= self.p.maker_timeout_s
                if not expired and not self._moved_away(m, price):
                    continue
                timed_out = expired  # otherwise: re-price at the new touch
                if not self._absorb(m, h, vm.cancel_order(m.inst, oid), seen, carry, rep):
                    failed = True
                break
            remaining -= seen.qty
            if timed_out:
                rep.messages.append(
                    f"maker order on {m.inst.key} timed out; {remaining:.6g} not traded"
                )
        if carry.qty * ref_mid > self.p.residual_tolerance_usd:
            self._unwind(m, carry.qty, rep, "sub-lot remainder")
        self._record_residual(legs, rep, ref_mid)
        rep.complete = (
            not rep.has_residual
            and abs(target_qty - rep.hedged_qty) * ref_mid <= self.p.residual_tolerance_usd
        )
        return rep

    def _absorb(
        self,
        m: _LegPlan,
        h: _LegPlan,
        st: OrderState,
        seen: _Seen,
        carry: _Carry,
        rep: ExecutionReport,
    ) -> bool:
        """Record new maker fills and hedge them. False if the hedge could not be done."""
        new = st.filled - seen.qty
        if new <= 1e-12:
            return True
        cost_now = st.filled * (st.avg_price or 0.0)
        price = (cost_now - seen.cost) / new
        rep.fills.append(Fill(m.inst, m.order_side, new, price, st.fee_usd - seen.fee, st.order_id))
        seen.qty, seen.cost, seen.fee = st.filled, cost_now, st.fee_usd
        want = new + carry.qty
        left = self._hedge(h, want, rep)
        rep.hedged_qty += want - left
        carry.qty = 0.0
        if self.venues[h.inst.venue].round_qty(h.inst, left) > 0:  # a full lot was refused
            self._unwind(m, left, rep, "hedge failed")
            return False
        carry.qty = left  # sub-lot remainder: fold into the next hedge
        return True

    def _moved_away(self, m: _LegPlan, our_price: float) -> bool:
        q = self._quotes([m])[0]
        best = q.bid if m.order_side == "buy" else q.ask
        drift = (best - our_price) if m.order_side == "buy" else (our_price - best)
        return drift / our_price * 1e4 > self.p.maker_requote_bps

    def _unwind(
        self, leg: _LegPlan, qty: float, rep: ExecutionReport, reason: str = "hedge failed"
    ) -> None:
        # undoing an opening order reduces the position; undoing a closing order re-opens it
        back = _LegPlan(
            leg.inst,
            "sell" if leg.order_side == "buy" else "buy",
            leg.inst.is_perp and not leg.reduce_only,
        )
        venue = self.venues[leg.inst.venue]
        left = venue.round_qty(leg.inst, qty)
        for attempt in range(self.p.hedge_retries):
            if left <= 0:
                break
            touch, _ = self._quotes([back])[0].touch(back.order_side)
            f = self._ioc(back, left, touch, self.p.hedge_slippage_bps * (attempt + 2))
            if f.qty > 0:
                rep.fills.append(f)
                left = venue.round_qty(leg.inst, left - f.qty)
        msg = f"{reason}: unwound {qty - left:.6g} of {leg.inst.key}"
        if left > 0:
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
