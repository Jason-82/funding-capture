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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import Candidate, Fill, Instrument, Side
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


@dataclass
class _LegPlan:
    inst: Instrument
    order_side: str
    reduce_only: bool


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
    def __init__(self, venues: dict[str, Venue], params: ExecutionParams | None = None):
        self.venues = venues
        self.p = params or ExecutionParams()

    # ------------------------------------------------------------------ public API
    def open(self, cand: Candidate, notional_usd: float) -> ExecutionReport:
        legs = [
            _LegPlan(cand.long, Side.LONG.order_side, False),
            _LegPlan(cand.short, Side.SHORT.order_side, False),
        ]
        quotes = self._quotes(legs)
        mid = (quotes[0].mid + quotes[1].mid) / 2
        return self._execute(legs, notional_usd / mid)

    def close(self, cand: Candidate, qty: float) -> ExecutionReport:
        legs = [
            _LegPlan(cand.long, Side.LONG.opposite.order_side, cand.long.is_perp),
            _LegPlan(cand.short, Side.SHORT.opposite.order_side, cand.short.is_perp),
        ]
        return self._execute(legs, qty)

    def trade_one(self, inst: Instrument, order_side: str, qty: float, reduce_only: bool) -> Fill:
        """Single IOC on one leg (used by the rebalancer)."""
        q = self.venues[inst.venue].fetch_quotes([inst])[inst.key]
        price, _ = q.touch(order_side)
        return self._ioc(
            _LegPlan(inst, order_side, reduce_only), qty, price, self.p.hedge_slippage_bps
        )

    # ------------------------------------------------------------------ internals
    def _quotes(self, legs: list[_LegPlan]):
        out = []
        for leg in legs:
            q = self.venues[leg.inst.venue].fetch_quotes([leg.inst]).get(leg.inst.key)
            if q is None:
                raise RuntimeError(f"no quote for {leg.inst.key}")
            out.append(q)
        return out

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

    def _execute(self, legs: list[_LegPlan], target_qty: float) -> ExecutionReport:
        rep = ExecutionReport()
        quotes = self._quotes(legs)
        # thinner touch (in USD) goes first: it's the leg most likely to under-fill
        depth = [q.touch(leg.order_side)[1] * q.mid for leg, q in zip(legs, quotes, strict=True)]
        first, second = (0, 1) if depth[0] <= depth[1] else (1, 0)
        a, b = legs[first], legs[second]
        va, vb = self.venues[a.inst.venue], self.venues[b.inst.venue]
        min_qty = max(va.min_qty(a.inst), vb.min_qty(b.inst))

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

            need = vb.round_qty(b.inst, fa.qty)
            for attempt in range(self.p.hedge_retries):
                if need <= 0:
                    break
                touch_b, _ = self._quotes([b])[0].touch(b.order_side)
                fb = self._ioc(b, need, touch_b, self.p.hedge_slippage_bps * (attempt + 1))
                if fb.qty > 0:
                    rep.fills.append(fb)
                    need = vb.round_qty(b.inst, need - fb.qty)
            rep.hedged_qty += max(0.0, fa.qty - need)
            remaining -= fa.qty
            if need * qb.mid > self.p.residual_tolerance_usd:
                self._unwind(a, need, rep)
                break  # the hedge venue is not cooperating; don't keep paying to retry

        self._record_residual(legs, rep, quotes[0].mid)
        rep.complete = (
            not rep.has_residual
            and abs(target_qty - rep.hedged_qty) * quotes[0].mid <= self.p.residual_tolerance_usd
        )
        return rep

    def _unwind(self, leg: _LegPlan, qty: float, rep: ExecutionReport) -> None:
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
        msg = f"hedge failed on pair; unwound {qty - left:.6g} of {leg.inst.key}"
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
