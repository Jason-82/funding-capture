"""Core domain types shared by the scanner, backtester and trading engine.

Conventions used everywhere in this package:

* Quantities are in *canonical asset units* (1.0 == one whole BTC, one whole PEPE),
  and prices are USD per canonical unit. Venue adapters convert contract sizes and
  "1000PEPE"-style multipliers at the boundary so nothing else has to.
* Funding rates are stored as published (fraction per settlement interval) together
  with the interval length; ``hourly`` / ``apr`` give normalised views.
* A positive funding rate means longs pay shorts.
* USDT, USDC and USD are treated as 1:1. That is an assumption, not a fact; the
  risk module watches stablecoin prices separately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

HOURS_PER_YEAR = 24 * 365
MS_PER_HOUR = 3_600_000


class Kind(StrEnum):
    SPOT = "spot"
    PERP = "perp"


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> Side:
        return Side.SHORT if self is Side.LONG else Side.LONG

    @property
    def order_side(self) -> str:
        """Order side that *opens* exposure on this side."""
        return "buy" if self is Side.LONG else "sell"


class Construction(StrEnum):
    # long spot + short perp: earns positive funding, no borrowing needed.
    SPOT_PERP = "spot_perp"
    # short spot (margin borrow) + long perp: earns negative funding minus borrow cost.
    REVERSE = "reverse_spot_perp"
    # long perp on the venue with lower funding, short perp where funding is higher.
    PERP_PERP = "perp_perp"


@dataclass(frozen=True)
class Instrument:
    venue: str
    symbol: str  # venue-native (ccxt) symbol, e.g. "BTC/USDT:USDT"
    asset: str  # canonical base asset, e.g. "BTC"
    kind: Kind
    quote: str = "USDT"
    multiplier: float = 1.0  # canonical units per venue base unit ("1000PEPE" -> 1000)
    contract_size: float = 1.0  # venue base units per contract (perps only)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"

    @property
    def is_perp(self) -> bool:
        return self.kind is Kind.PERP


@dataclass(frozen=True)
class Leg:
    instrument: Instrument
    side: Side


@dataclass(frozen=True)
class Candidate:
    """A specific delta-neutral pairing that could be opened."""

    construction: Construction
    asset: str
    long: Instrument
    short: Instrument

    @property
    def key(self) -> str:
        return f"{self.construction}|{self.asset}|L={self.long.key}|S={self.short.key}"

    @property
    def legs(self) -> tuple[Leg, Leg]:
        return Leg(self.long, Side.LONG), Leg(self.short, Side.SHORT)

    @property
    def venues(self) -> set[str]:
        return {self.long.venue, self.short.venue}

    def describe(self) -> str:
        return (
            f"{self.construction:<17} {self.asset:<10} "
            f"long {self.long.venue}:{self.long.kind}  short {self.short.venue}:{self.short.kind}"
        )


@dataclass(frozen=True)
class FundingSnapshot:
    instrument: Instrument
    rate: float  # fraction per settlement interval (current / predicted for next settlement)
    interval_hours: float
    ts: int  # ms when observed
    next_funding_ts: int | None = None
    mark_price: float | None = None  # USD per canonical unit
    index_price: float | None = None

    @property
    def hourly(self) -> float:
        return self.rate / self.interval_hours

    @property
    def apr(self) -> float:
        return self.hourly * HOURS_PER_YEAR


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    bid: float  # USD per canonical unit
    ask: float
    bid_size: float = math.inf  # canonical units available at the touch
    ask_size: float = math.inf
    ts: int = 0
    volume_usd_24h: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 1e4 if self.mid > 0 else math.inf

    def touch(self, order_side: str) -> tuple[float, float]:
        """(price, size) you would trade against when sending ``order_side``."""
        return (self.ask, self.ask_size) if order_side == "buy" else (self.bid, self.bid_size)


@dataclass
class Fill:
    instrument: Instrument
    order_side: str  # "buy" | "sell"
    qty: float  # canonical units actually filled
    price: float  # average USD per canonical unit
    fee_usd: float
    order_id: str | None = None

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass
class PerpPositionInfo:
    """What a venue reports about an open perp position."""

    instrument: Instrument
    qty: float  # signed canonical units, + long / - short
    entry_price: float | None = None
    mark_price: float | None = None
    liquidation_price: float | None = None


@dataclass
class PairPosition:
    """An open (or closed) delta-neutral pair owned by the bot."""

    id: str
    candidate: Candidate
    qty: float  # canonical units on each leg
    long_entry: float
    short_entry: float
    opened_ms: int
    entry_fees_usd: float = 0.0
    exit_fees_usd: float = 0.0
    funding_usd: float = 0.0
    borrow_usd: float = 0.0
    closed_ms: int | None = None
    long_exit: float | None = None  # average exit prices of the last close
    short_exit: float | None = None
    realized_price_usd: float = 0.0  # P&L from the legs' price moves on closed quantity
    borrow_accrued_ms: int | None = None  # borrow cost accrued up to this time
    funding_cursor_ms: dict[str, int] = field(default_factory=dict)  # leg key -> last paid ts
    notes: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.closed_ms is None

    @property
    def entry_notional(self) -> float:
        return self.qty * (self.long_entry + self.short_entry) / 2

    @property
    def net_pnl_usd(self) -> float:
        return (
            self.funding_usd
            - self.borrow_usd
            - self.entry_fees_usd
            - self.exit_fees_usd
            + self.realized_price_usd
        )
