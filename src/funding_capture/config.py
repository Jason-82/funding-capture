"""YAML config -> typed settings. Unknown keys are errors (typos should not be silent)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .backtest import BacktestConfig
from .costs import DEFAULT_FEES, CostModel, VenueFees
from .engine import EngineConfig
from .execution import ExecutionParams
from .forecast import ForecastParams
from .models import HOURS_PER_YEAR, Construction
from .opportunities import StrategyParams
from .risk import RiskLimits


@dataclass(frozen=True)
class VenueSettings:
    name: str
    ccxt_id: str | None = None
    quotes: tuple[str, ...] = ("USDT", "USDC")  # most preferred first
    kinds: tuple[str, ...] = ("spot", "perp")  # e.g. [spot] to use a venue only for spot legs
    options: dict = field(default_factory=dict)  # passed to the ccxt constructor
    paper_leverage: float = 2.0
    # live only: refresh a cancel-all-orders-after timer of this many seconds every loop,
    # so resting maker orders die with the bot (it cancels ALL open orders on the account)
    dead_man_seconds: int = 0


@dataclass
class Settings:
    mode: str = "paper"
    data_dir: str = "data"
    venues: list[VenueSettings] = field(default_factory=list)
    costs: CostModel = field(default_factory=CostModel)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    portfolio: EngineConfig = field(default_factory=EngineConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    execution: ExecutionParams = field(default_factory=ExecutionParams)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)


def _build(cls, data: dict | None, section: str, **extra):
    data = dict(data or {})
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"[{section}] unknown keys: {sorted(unknown)}; allowed: {sorted(names)}")
    for f in dataclasses.fields(cls):
        if f.name in data and isinstance(data[f.name], list):
            data[f.name] = tuple(data[f.name])
    return cls(**{**data, **extra})


def load_settings(path: str | Path | None) -> Settings:
    raw = yaml.safe_load(Path(path).read_text()) if path else {}
    raw = raw or {}
    known = {f.name for f in dataclasses.fields(Settings)} | {"fees"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown top-level config keys: {sorted(unknown)}")

    venues = [
        _build(VenueSettings, v, f"venues.{name}", name=name)
        for name, v in (raw.get("venues") or {"binance": {}, "bybit": {}, "okx": {}}).items()
    ]

    fees = dict(DEFAULT_FEES)
    for name, f in (raw.get("fees") or {}).items():
        base = fees.get(name, VenueFees())
        fees[name] = dataclasses.replace(base, **f)
    costs = _build(CostModel, raw.get("costs"), "costs", fees=fees)

    strat_raw = dict(raw.get("strategy") or {})
    fc_raw = dict(strat_raw.pop("forecast", None) or {})
    if "anchor_apr" in fc_raw:
        fc_raw["anchor_hourly"] = fc_raw.pop("anchor_apr") / HOURS_PER_YEAR
    forecast = _build(ForecastParams, fc_raw, "strategy.forecast")
    if "constructions" in strat_raw:
        strat_raw["constructions"] = [Construction(c) for c in strat_raw["constructions"]]
    strategy = _build(StrategyParams, strat_raw, "strategy", forecast=forecast)

    return Settings(
        mode=raw.get("mode", "paper"),
        data_dir=raw.get("data_dir", "data"),
        venues=venues,
        costs=costs,
        strategy=strategy,
        portfolio=_build(EngineConfig, raw.get("portfolio"), "portfolio"),
        risk=_build(RiskLimits, raw.get("risk"), "risk"),
        execution=_build(ExecutionParams, raw.get("execution"), "execution"),
        backtest=_build(BacktestConfig, raw.get("backtest"), "backtest"),
    )
