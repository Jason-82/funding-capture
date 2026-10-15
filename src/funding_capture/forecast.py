"""Funding forecasts.

Funding is persistent over hours to days but mean-reverting: extreme prints decay
back toward the venue baseline (most venues add a 0.01%/8h interest component, so
"neutral" is ~11% APR, not zero). The forecast used everywhere is:

    level   = EWMA of observed hourly funding (optionally blended with the live
              predicted rate for the next settlement)
    E[avg over next H hours] = anchor + (level - anchor) * (1 - phi^H) / (H * (1 - phi))
    with phi = 0.5 ** (1 / reversion_halflife_hours)

That is the mean of an AR(1) path starting at ``level``. Applying it per leg and
differencing makes perp/perp spreads decay toward zero, which is the behaviour we want
to be conservative about.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

BASELINE_HOURLY = 0.0001 / 8  # 0.01% per 8h


@dataclass(frozen=True)
class ForecastParams:
    halflife_hours: float = 24.0
    reversion_halflife_hours: float = 72.0
    anchor_hourly: float = BASELINE_HOURLY
    # None: revert toward the global baseline above. A number: revert toward each market's
    # own slow EWMA with this half-life instead (lets persistent venue spreads count).
    anchor_halflife_hours: float | None = None
    live_weight: float = 0.5  # weight on the live predicted rate when one is available


def horizon_average(level, params: ForecastParams, horizon_hours: float, anchor=None):
    """Expected mean hourly funding over the next ``horizon_hours`` (float or ndarray)."""
    if params.reversion_halflife_hours <= 0 or math.isinf(params.reversion_halflife_hours):
        return level
    a = params.anchor_hourly if anchor is None else anchor
    phi = 0.5 ** (1.0 / params.reversion_halflife_hours)
    h = max(horizon_hours, 1.0)
    factor = (1 - phi**h) / (h * (1 - phi))
    return a + (level - a) * factor


def ewma_frame(known_hourly: pd.DataFrame, halflife_hours: float) -> pd.DataFrame:
    """Column-wise EWMA over an hourly grid, skipping gaps before listing."""
    return (
        known_hourly.ewm(halflife=halflife_hours, ignore_na=True).mean().where(known_hourly.notna())
    )


@dataclass
class _EwmaState:
    fast: float
    slow: float
    ts_ms: int


@dataclass
class StreamingForecaster:
    """Time-decayed EWMAs per instrument for the live/paper engine."""

    params: ForecastParams = field(default_factory=ForecastParams)
    _state: dict[str, _EwmaState] = field(default_factory=dict)

    def observe(self, key: str, hourly: float, ts_ms: int) -> None:
        st = self._state.get(key)
        if st is None:
            self._state[key] = _EwmaState(hourly, hourly, ts_ms)
            return
        if ts_ms <= st.ts_ms:
            return
        dt_h = (ts_ms - st.ts_ms) / 3_600_000
        decay = 0.5 ** (dt_h / self.params.halflife_hours)
        st.fast = decay * st.fast + (1 - decay) * hourly
        if self.params.anchor_halflife_hours:
            slow_decay = 0.5 ** (dt_h / self.params.anchor_halflife_hours)
            st.slow = slow_decay * st.slow + (1 - slow_decay) * hourly
        st.ts_ms = ts_ms

    def seed(self, key: str, history: list[tuple[int, float]]) -> None:
        """Replay settled (ts_ms, hourly_rate) history, oldest first."""
        for ts, hourly in sorted(history):
            self.observe(key, hourly, ts)

    def level(self, key: str, live_hourly: float | None = None) -> float | None:
        st = self._state.get(key)
        if st is None:
            return live_hourly
        if live_hourly is None:
            return st.fast
        w = self.params.live_weight
        return w * live_hourly + (1 - w) * st.fast

    def forecast(self, key: str, horizon_hours: float, live_hourly: float | None = None):
        lvl = self.level(key, live_hourly)
        if lvl is None:
            return None
        st = self._state.get(key)
        anchor = st.slow if (st is not None and self.params.anchor_halflife_hours) else None
        return float(horizon_average(np.float64(lvl), self.params, horizon_hours, anchor))

    def to_dict(self) -> dict:
        return {k: [s.fast, s.slow, s.ts_ms] for k, s in self._state.items()}

    def load_dict(self, data: dict) -> None:
        self._state = {k: _EwmaState(float(f), float(sl), int(t)) for k, (f, sl, t) in data.items()}
