"""Venue interface. All quantities are canonical asset units, prices USD per unit."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from ..models import FundingSnapshot, Instrument, PerpPositionInfo, Quote


class Venue(ABC):
    name: str

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

    @abstractmethod
    def fetch_perp_positions(self) -> dict[str, PerpPositionInfo]: ...

    @abstractmethod
    def fetch_spot_balance(self, inst: Instrument) -> float: ...

    def fetch_funding_payments(self, inst: Instrument, since_ms: int) -> list[tuple[int, float]]:
        """Funding actually paid/received on ``inst`` since ``since_ms``: ``[(ts, usd)]``."""
        return []

    def tick(self, now_ms: int) -> None:  # noqa: B027 - optional hook, no-op by default
        """Hook for simulated venues to advance time (settle funding, etc.)."""


def floor_to_step(qty: float, step: float | None) -> float:
    if not step or step <= 0:
        return qty
    n = math.floor(qty / step + 1e-9)
    return round(n * step, 12)
