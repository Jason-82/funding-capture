"""On-disk store of historical funding settlements, plus a ccxt-backed downloader.

Layout::

    <root>/<venue>/instruments.json          list of instrument dicts (perps and spots)
    <root>/<venue>/funding/<symbol>.csv      ts_ms,rate  (one row per settlement)
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .models import Instrument, Kind
from .rates import DEFAULT_INTERVAL_HOURS, infer_interval_hours

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(symbol: str) -> str:
    return _UNSAFE.sub("_", symbol)


def instrument_to_dict(inst: Instrument) -> dict:
    d = asdict(inst)
    d["kind"] = inst.kind.value
    return d


def instrument_from_dict(d: dict) -> Instrument:
    return Instrument(**{**d, "kind": Kind(d["kind"])})


class HistoryStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    # -- instruments ---------------------------------------------------------------
    def save_instruments(self, venue: str, instruments: list[Instrument]) -> None:
        path = self.root / venue / "instruments.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([instrument_to_dict(i) for i in instruments], indent=1))

    def load_instruments(self, venue: str) -> list[Instrument]:
        path = self.root / venue / "instruments.json"
        if not path.exists():
            return []
        return [instrument_from_dict(d) for d in json.loads(path.read_text())]

    def venues(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "instruments.json").exists())

    # -- funding -------------------------------------------------------------------
    def _funding_path(self, inst: Instrument) -> Path:
        return self.root / inst.venue / "funding" / f"{_safe(inst.symbol)}.csv"

    def load_funding(self, inst: Instrument) -> list[tuple[int, float]]:
        path = self._funding_path(inst)
        if not path.exists():
            return []
        with path.open() as f:
            return [(int(r["ts_ms"]), float(r["rate"])) for r in csv.DictReader(f)]

    def append_funding(self, inst: Instrument, rows: list[tuple[int, float]]) -> int:
        """Merge rows into the stored series; returns number of new settlements."""
        existing = dict(self.load_funding(inst))
        before = len(existing)
        for ts, rate in rows:
            existing[int(ts)] = float(rate)
        path = self._funding_path(inst)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts_ms", "rate"])
            for ts in sorted(existing):
                w.writerow([ts, repr(existing[ts])])
        return len(existing) - before

    def load_all(self, venues: list[str] | None = None) -> tuple[list[Instrument], dict]:
        """Return (instruments, {instrument_key: DataFrame[ts_ms, rate]})."""
        instruments: list[Instrument] = []
        series: dict[str, pd.DataFrame] = {}
        for venue in venues or self.venues():
            for inst in self.load_instruments(venue):
                instruments.append(inst)
                if inst.kind is Kind.PERP:
                    rows = self.load_funding(inst)
                    if rows:
                        series[inst.key] = pd.DataFrame(rows, columns=["ts_ms", "rate"])
        return instruments, series


def interval_for(inst: Instrument, rows: list[tuple[int, float]]) -> float:
    inferred = infer_interval_hours([ts for ts, _ in rows[-200:]])
    return inferred or DEFAULT_INTERVAL_HOURS.get(inst.venue, 8.0)


def download_funding_history(
    venue,  # venues.ccxt_venue.CcxtVenue (kept untyped to avoid importing ccxt here)
    store: HistoryStore,
    since_ms: int,
    until_ms: int | None = None,
    assets: set[str] | None = None,
    min_volume_usd: float = 0.0,
    max_markets: int | None = None,
) -> dict[str, int]:
    """Download settled funding for a venue's perps into ``store``; resumes per symbol."""
    instruments = venue.load_instruments()
    if assets:
        instruments = [i for i in instruments if i.asset in assets]
    perps = [i for i in instruments if i.kind is Kind.PERP]
    if min_volume_usd > 0 or max_markets:
        vols = venue.fetch_volumes(perps)
        perps = [p for p in perps if vols.get(p.key, 0.0) >= min_volume_usd]
        perps.sort(key=lambda p: -vols.get(p.key, 0.0))
        if max_markets:
            perps = perps[:max_markets]
    keep_assets = {p.asset for p in perps}
    store.save_instruments(
        venue.name,
        perps + [i for i in instruments if i.kind is Kind.SPOT and i.asset in keep_assets],
    )
    added: dict[str, int] = {}
    for n, inst in enumerate(perps, 1):
        existing = store.load_funding(inst)
        start = existing[-1][0] + 1 if existing else since_ms
        try:
            rows = venue.fetch_funding_history(inst, start, until_ms)
        except Exception as exc:  # one bad market should not kill the whole download
            log.warning("%s %s: history fetch failed: %s", venue.name, inst.symbol, exc)
            continue
        added[inst.key] = store.append_funding(inst, rows)
        log.info("[%d/%d] %s %s +%d", n, len(perps), venue.name, inst.symbol, added[inst.key])
    return added
