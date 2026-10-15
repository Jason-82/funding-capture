"""Funding-rate helpers: interval handling and carry arithmetic."""

from __future__ import annotations

import re

from .models import HOURS_PER_YEAR, Candidate, Kind

# Settlement interval used when a venue does not report one per market.
DEFAULT_INTERVAL_HOURS: dict[str, float] = {
    "hyperliquid": 1.0,
    "dydx": 1.0,
    "paradex": 8.0,
    "binance": 8.0,
    "bybit": 8.0,
    "okx": 8.0,
    "bitget": 8.0,
    "gate": 8.0,
    "kucoinfutures": 8.0,
    "mexc": 8.0,
    "bingx": 8.0,
}

_INTERVAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mhd])\s*$")


def parse_interval_hours(value: str | float | int | None) -> float | None:
    """Parse ccxt-style intervals ("8h", "60m", "1d") or plain hour numbers."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    m = _INTERVAL_RE.match(value)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    return {"m": n / 60, "h": n, "d": n * 24}[unit]


def infer_interval_hours(timestamps_ms: list[int]) -> float | None:
    """Median spacing between consecutive settlements, rounded to whole hours."""
    if len(timestamps_ms) < 3:
        return None
    ts = sorted(timestamps_ms)
    gaps = sorted(b - a for a, b in zip(ts, ts[1:], strict=False) if b > a)
    if not gaps:
        return None
    median = gaps[len(gaps) // 2] / 3_600_000
    return max(1.0, round(median))


def apr(hourly: float) -> float:
    return hourly * HOURS_PER_YEAR


def leg_funding_hourly(kind: Kind, side_sign: int, hourly_rate: float) -> float:
    """Funding *received* per hour per $ notional by one leg (spot legs earn nothing)."""
    if kind is not Kind.PERP:
        return 0.0
    return -side_sign * hourly_rate


def candidate_carry_hourly(
    cand: Candidate, long_hourly: float, short_hourly: float, borrow_hourly: float = 0.0
) -> float:
    """Net funding received per hour per $ notional for a pair, before trading costs."""
    return (
        leg_funding_hourly(cand.long.kind, +1, long_hourly)
        + leg_funding_hourly(cand.short.kind, -1, short_hourly)
        - borrow_hourly
    )


def breakeven_hours(round_trip_cost: float, carry_hourly: float) -> float:
    return round_trip_cost / carry_hourly if carry_hourly > 0 else float("inf")
