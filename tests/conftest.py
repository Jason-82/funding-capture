"""Test fixtures: an in-memory market-data venue and synthetic funding histories.

Synthetic data here exists only to check mechanics against hand-computed answers.
It says nothing about whether the strategy makes money.
"""

from __future__ import annotations

import pandas as pd
import pytest

from funding_capture.models import FundingSnapshot, Instrument, Kind, Quote
from funding_capture.venues.base import Venue, floor_to_step

HOUR = 3_600_000
T0 = 1_700_000_000_000 // HOUR * HOUR  # an hour boundary


def perp(venue: str, asset: str = "BTC", quote: str = "USDT") -> Instrument:
    return Instrument(venue, f"{asset}/{quote}:{quote}", asset, Kind.PERP, quote)


def spot(venue: str, asset: str = "BTC", quote: str = "USDT") -> Instrument:
    return Instrument(venue, f"{asset}/{quote}", asset, Kind.SPOT, quote)


class FakeVenue(Venue):
    """Market data you set by hand. Trading is done by wrapping it in PaperVenue."""

    def __init__(self, name: str, instruments: list[Instrument], lot: float = 0.001):
        self.name = name
        self.instruments = instruments
        self.lot = lot
        self.funding: dict[str, FundingSnapshot] = {}
        self.quotes: dict[str, Quote] = {}
        self.history: dict[str, list[tuple[int, float]]] = {}
        self.fail = False

    def set_quote(self, inst, bid, ask, size=1e9, volume=1e9, ts=T0):
        self.quotes[inst.key] = Quote(inst, bid, ask, size, size, ts, volume)

    def set_funding(self, inst, rate, interval=8.0, next_ts=None, mark=None, ts=T0):
        self.funding[inst.key] = FundingSnapshot(inst, rate, interval, ts, next_ts, mark)

    def load_instruments(self):
        return list(self.instruments)

    def fetch_funding(self, instruments=None):
        if self.fail:
            raise ConnectionError("venue down")
        keys = {i.key for i in instruments} if instruments is not None else None
        return {k: v for k, v in self.funding.items() if keys is None or k in keys}

    def fetch_quotes(self, instruments):
        if self.fail:
            raise ConnectionError("venue down")
        keys = {i.key for i in instruments}
        return {k: v for k, v in self.quotes.items() if k in keys}

    def fetch_funding_history(self, inst, since_ms, until_ms=None):
        return [r for r in self.history.get(inst.key, []) if r[0] >= since_ms]

    def round_qty(self, inst, qty):
        return floor_to_step(qty, self.lot)

    def place_ioc(self, *a, **k):
        raise NotImplementedError("wrap in PaperVenue")

    def fetch_perp_positions(self):
        return {}

    def fetch_spot_balance(self, inst):
        return 0.0


def settlement_frame(start_ms: int, hours: int, interval_h: int, rate_fn) -> pd.DataFrame:
    """Settlement rows every ``interval_h`` hours; ``rate_fn(i)`` gives the i-th rate."""
    n = hours // interval_h
    ts = [start_ms + (i + 1) * interval_h * HOUR for i in range(n)]
    return pd.DataFrame({"ts_ms": ts, "rate": [rate_fn(i) for i in range(n)]})


@pytest.fixture
def clock():
    class Clock:
        def __init__(self):
            self.ms = T0

        def __call__(self):
            return self.ms / 1000

        def advance(self, hours=0.0, minutes=0.0):
            self.ms += int(hours * HOUR + minutes * 60_000)

    return Clock()
