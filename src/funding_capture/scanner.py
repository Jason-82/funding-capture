"""One-shot opportunity scan across venues (public data only, no API keys needed)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .costs import CostModel
from .data import HistoryStore
from .engine import Engine, EngineConfig
from .opportunities import Scored, StrategyParams, passes_entry
from .risk import RiskLimits, price_divergence
from .venues.base import Venue


@dataclass(frozen=True)
class ScanRow:
    scored: Scored
    volume_usd: float | None  # min 24h volume across the two legs
    divergence: float | None  # leg mid-price disagreement
    tradeable: bool
    passes: bool


def scan(
    venues: dict[str, Venue],
    strategy: StrategyParams,
    costs: CostModel,
    limits: RiskLimits,
    cfg: EngineConfig,
    store: HistoryStore | None = None,
) -> list[ScanRow]:
    eng = Engine(venues, strategy, costs, None, limits, cfg, store=store)  # type: ignore[arg-type]
    eng.bootstrap(load_state=False)
    now = int(time.time() * 1000)
    eng.refresh(now, with_positions=False)
    rows = []
    for s in eng.score_all().values():
        if math.isnan(s.net_apr_on_capital):
            continue
        c = s.candidate
        ql, qs = eng.quotes.get(c.long.key), eng.quotes.get(c.short.key)
        vols = [q.volume_usd_24h for q in (ql, qs) if q is not None and q.volume_usd_24h]
        rows.append(
            ScanRow(
                scored=s,
                volume_usd=min(vols) if len(vols) == 2 else None,
                divergence=price_divergence(ql.mid if ql else None, qs.mid if qs else None),
                tradeable=eng.tradeable(c, now),
                passes=passes_entry(s, strategy),
            )
        )
    rows.sort(key=lambda r: -r.scored.net_apr_on_capital)
    return rows


def format_rows(rows: list[ScanRow], top: int = 25, only_tradeable: bool = True) -> str:
    hdr = (
        f"{'construction':<17} {'asset':<9} {'long leg':<19} {'short leg':<19} "
        f"{'gross now':>9} {'fcst':>8} {'net/cap':>8} {'b/e h':>6} {'vol $M':>7} {'div':>6}  ok"
    )
    out = [hdr, "-" * len(hdr)]
    shown = 0
    for r in rows:
        if only_tradeable and not r.tradeable:
            continue
        s, c = r.scored, r.scored.candidate
        out.append(
            f"{c.construction:<17} {c.asset:<9} "
            f"{c.long.venue + ':' + c.long.kind:<19} {c.short.venue + ':' + c.short.kind:<19} "
            f"{s.gross_apr_now:>9.1%} {s.carry_fcst_hourly * 8760:>8.1%} "
            f"{s.net_apr_on_capital:>8.1%} {min(s.breakeven_hours, 9999):>6.0f} "
            f"{(r.volume_usd or 0) / 1e6:>7.1f} "
            f"{(r.divergence or 0):>6.2%}  {'*' if r.passes else ''}"
        )
        shown += 1
        if shown >= top:
            break
    out.append(
        "gross now = current funding carry APR on notional; fcst = forecast carry over the "
        "horizon; net/cap = forecast APR on capital after round-trip costs; "
        "* = passes entry rules"
    )
    return "\n".join(out)
