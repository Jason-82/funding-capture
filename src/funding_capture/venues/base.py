"""Venue interface. All quantities are canonical asset units, prices USD per unit."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from ..models import FundingSnapshot, Instrument, OrderState, PerpPositionInfo, Quote


class Venue(ABC):
    name: str
    # False when the venue has no API for funding actually paid; the engine then estimates
    # it from observed rates and positions.
    reports_funding: bool = True

    # -- market data -----------------------------------------------------------------
    @abstractmethod
    def load_instruments(self) -> list[Instrument]: ...

    @abstractmethod
    def fetch_funding(
        self, instruments: list[Instrument] | None = None
    ) -> dict[str, FundingSnapshot]:
        """Current/predicted funding for perps, keyed by ``Instrument.key``."""

    @abstractmethod
    def fetch_quotes(self, instruments: list[Instrument]) -> dict[str, Quote]: ...

    def fetch_touch(self, inst: Instrument) -> Quote:
        """Best bid/ask with sizes for one instrument, as fresh as possible (for trading)."""
        q = self.fetch_quotes([inst]).get(inst.key)
        if q is None:
            raise RuntimeError(f"no quote for {inst.key}")
        return q

    def fetch_volumes(self, instruments: list[Instrument]) -> dict[str, float]:
        quotes = self.fetch_quotes(instruments)
        return {k: q.volume_usd_24h or 0.0 for k, q in quotes.items()}

    def fetch_funding_history(
        self, inst: Instrument, since_ms: int, until_ms: int | None = None
    ) -> list[tuple[int, float]]:
        """Settled funding as ``[(ts_ms, rate_per_interval), ...]`` oldest first."""
        raise NotImplementedError

    # -- trading ---------------------------------------------------------------------
    def round_qty(self, inst: Instrument, qty: float) -> float:
        return qty

    def min_qty(self, inst: Instrument) -> float:
        return 0.0

    def min_notional(self, inst: Instrument) -> float:
        """Smallest order value in USD the venue accepts (Hyperliquid: $10)."""
        return 0.0

    @abstractmethod
    def place_ioc(
        self,
        inst: Instrument,
        order_side: str,
        qty: float,
        limit_price: float,
        reduce_only: bool = False,
    ):
        """Immediate-or-cancel limit order; returns a ``Fill`` (qty may be 0)."""

    # Resting (maker) orders. Venues that can't do post-only leave these unimplemented and
    # the executor falls back to IOC on them.
    supports_post_only: bool = False

    def place_post_only(
        self, inst: Instrument, order_side: str, qty: float, price: float, reduce_only=False
    ) -> str | None:
        """Rest a post-only limit; returns the order id, or None if it would have crossed."""
        raise NotImplementedError

    def fetch_order_state(self, inst: Instrument, order_id: str) -> OrderState:
        raise NotImplementedError

    def cancel_order(self, inst: Instrument, order_id: str) -> OrderState:
        """Cancel (if still open) and return the final state, including late fills."""
        raise NotImplementedError

    @abstractmethod
    def fetch_perp_positions(self) -> dict[str, PerpPositionInfo]: ...

    @abstractmethod
    def fetch_spot_balance(self, inst: Instrument) -> float: ...

    def fetch_funding_payments(self, inst: Instrument, since_ms: int) -> list[tuple[int, float]]:
        """Funding actually paid/received on ``inst`` since ``since_ms``: ``[(ts, usd)]``."""
        return []

    def tick(self, now_ms: int) -> None:  # noqa: B027 - optional hook, no-op by default
        """Hook for simulated venues to advance time (settle funding, etc.)."""

    def heartbeat(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Called every live loop; e.g. refreshes a cancel-all-orders dead-man's switch."""

    def margin_buffer(self) -> float | None:
        """Account-wide distance to liquidation as a fraction, for venues that only report
        margin per account (no per-position liquidation price). None if unknown."""
        return None


def floor_to_step(qty: float, step: float | None) -> float:
    if not step or step <= 0:
        return qty
    n = math.floor(qty / step + 1e-9)
    return round(n * step, 12)
