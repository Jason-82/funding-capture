"""Paper-trading venue: real market data from a wrapped venue, simulated fills and funding.

Fills: an IOC order fills against the current touch if its limit crosses it, up to the
displayed touch size (scaled by ``touch_fraction``), at the touch price, paying the
venue's taker fee. Funding: when a perp's settlement time passes, each open position is
paid/charged the last rate observed before the settlement, on its notional at mark.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from ..costs import VenueFees
from ..models import Fill, FundingSnapshot, Instrument, Kind, PerpPositionInfo, Quote
from .base import Venue, floor_to_step


@dataclass
class _Book:
    qty: float = 0.0  # signed canonical units
    entry: float = 0.0  # average entry price of the open quantity


@dataclass
class PaperState:
    books: dict[str, _Book] = field(default_factory=dict)
    payments: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    pending: dict[str, tuple[float, int]] = field(default_factory=dict)  # key -> (rate, next_ts)
    fees_usd: float = 0.0


class PaperVenue(Venue):
    def __init__(
        self,
        data: Venue,
        fees: VenueFees | None = None,
        leverage: float = 2.0,
        maintenance_margin: float = 0.01,
        touch_fraction: float = 1.0,
        lot_step: float | None = None,
    ):
        self.name = data.name
        self.data = data
        self.fees = fees or VenueFees()
        self.leverage = leverage
        self.mmr = maintenance_margin
        self.touch_fraction = touch_fraction
        self.lot_step = lot_step
        self.state = PaperState()
        self._ids = itertools.count(1)
        self._instruments: dict[str, Instrument] = {}

    # -- data: delegate ---------------------------------------------------------------
    def load_instruments(self) -> list[Instrument]:
        insts = self.data.load_instruments()
        self._instruments = {i.key: i for i in insts}
        return insts

    def fetch_funding(self, instruments=None) -> dict[str, FundingSnapshot]:
        return self.data.fetch_funding(instruments)

    def fetch_quotes(self, instruments) -> dict[str, Quote]:
        return self.data.fetch_quotes(instruments)

    def fetch_volumes(self, instruments) -> dict[str, float]:
        return self.data.fetch_volumes(instruments)

    def fetch_funding_history(self, inst, since_ms, until_ms=None):
        return self.data.fetch_funding_history(inst, since_ms, until_ms)

    def round_qty(self, inst: Instrument, qty: float) -> float:
        if self.lot_step:
            return floor_to_step(qty, self.lot_step)
        return self.data.round_qty(inst, qty)

    def min_qty(self, inst: Instrument) -> float:
        return self.data.min_qty(inst)

    # -- simulated trading ------------------------------------------------------------
    def _book(self, inst: Instrument) -> _Book:
        return self.state.books.setdefault(inst.key, _Book())

    def place_ioc(self, inst, order_side, qty, limit_price, reduce_only=False) -> Fill:
        qty = self.round_qty(inst, qty)
        quote = self.data.fetch_quotes([inst]).get(inst.key)
        empty = Fill(inst, order_side, 0.0, limit_price, 0.0, None)
        if quote is None or qty <= 0:
            return empty
        price, size = quote.touch(order_side)
        crosses = price <= limit_price if order_side == "buy" else price >= limit_price
        if not crosses:
            return empty
        book = self._book(inst)
        sign = 1 if order_side == "buy" else -1
        fill_qty = min(qty, size * self.touch_fraction)
        if reduce_only:
            reducible = max(0.0, -sign * book.qty)
            fill_qty = min(fill_qty, reducible)
        fill_qty = self.round_qty(inst, fill_qty)
        if fill_qty <= 0:
            return empty
        fee = fill_qty * price * self.fees.fee(inst.kind, taker=True)
        self._apply(book, sign * fill_qty, price)
        self.state.fees_usd += fee
        if inst.is_perp and inst.key not in self.state.pending:
            snap = self.data.fetch_funding([inst]).get(inst.key)
            if snap is not None:
                self.state.pending[inst.key] = (snap.rate, snap.next_funding_ts)
        return Fill(inst, order_side, fill_qty, price, fee, f"paper-{next(self._ids)}")

    @staticmethod
    def _apply(book: _Book, signed_qty: float, price: float) -> None:
        new = book.qty + signed_qty
        if book.qty == 0 or (book.qty > 0) == (signed_qty > 0):
            # opening or adding: weighted average entry
            book.entry = (abs(book.qty) * book.entry + abs(signed_qty) * price) / abs(new)
        elif abs(signed_qty) > abs(book.qty):
            book.entry = price  # flipped through zero
        book.qty = 0.0 if math.isclose(new, 0.0, abs_tol=1e-12) else new

    def fetch_perp_positions(self) -> dict[str, PerpPositionInfo]:
        out: dict[str, PerpPositionInfo] = {}
        perps = [self._instruments.get(k) for k, b in self.state.books.items() if b.qty != 0]
        perps = [i for i in perps if i is not None and i.kind is Kind.PERP]
        snaps = self.data.fetch_funding(perps) if perps else {}
        for inst in perps:
            book = self.state.books[inst.key]
            if book.qty > 0:
                liq = book.entry * (1 - 1 / self.leverage + self.mmr)
            else:
                liq = book.entry * (1 + 1 / self.leverage - self.mmr)
            snap = snaps.get(inst.key)
            out[inst.key] = PerpPositionInfo(
                inst, book.qty, book.entry, snap.mark_price if snap else None, liq
            )
        return out

    def fetch_spot_balance(self, inst: Instrument) -> float:
        return self.state.books.get(inst.key, _Book()).qty

    def fetch_funding_payments(self, inst: Instrument, since_ms: int) -> list[tuple[int, float]]:
        return [(t, v) for t, v in self.state.payments.get(inst.key, []) if t >= since_ms]

    def tick(self, now_ms: int) -> None:
        """Settle funding for any perp whose settlement time has passed."""
        held = [
            self._instruments[k]
            for k, b in self.state.books.items()
            if b.qty != 0 and k in self._instruments and self._instruments[k].is_perp
        ]
        self.state.pending = {
            k: v for k, v in self.state.pending.items() if k in {i.key for i in held}
        }
        if not held:
            return
        snaps = self.data.fetch_funding(held)
        for inst in held:
            book = self.state.books[inst.key]
            pending = self.state.pending.get(inst.key)
            snap = snaps.get(inst.key)
            if pending is not None:
                rate, settle_ts = pending
                paid = self.state.payments.get(inst.key)
                already = bool(paid) and paid[-1][0] >= (settle_ts or 0)
                if settle_ts is not None and now_ms >= settle_ts and not already:
                    mark = (snap.mark_price if snap and snap.mark_price else None) or book.entry
                    amount = -rate * book.qty * mark  # longs pay positive funding
                    self.state.payments.setdefault(inst.key, []).append((settle_ts, amount))
            if snap is not None:
                self.state.pending[inst.key] = (snap.rate, snap.next_funding_ts)

    # -- persistence ------------------------------------------------------------------
    def to_dict(self) -> dict:
        s = self.state
        return {
            "books": {k: [b.qty, b.entry] for k, b in s.books.items() if b.qty != 0},
            "payments": s.payments,
            "pending": s.pending,
            "fees_usd": s.fees_usd,
        }

    def load_dict(self, d: dict) -> None:
        self.state = PaperState(
            books={k: _Book(q, e) for k, (q, e) in d.get("books", {}).items()},
            payments={k: [tuple(p) for p in v] for k, v in d.get("payments", {}).items()},
            pending={k: tuple(v) for k, v in d.get("pending", {}).items()},
            fees_usd=d.get("fees_usd", 0.0),
        )
