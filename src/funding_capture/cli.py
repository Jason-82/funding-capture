"""Command line: fcap check | scan | fetch | backtest | sweep | paper | live | status."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

from .config import Settings, load_settings

log = logging.getLogger("funding_capture")


def load_env_file(path: str) -> int:
    """Load KEY=VALUE lines into the environment (existing variables win). Returns count."""
    import os
    from pathlib import Path

    p = Path(path).expanduser()
    if not p.exists():
        return 0
    n = 0
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            n += 1
    return n


def _ms(date: str | None) -> int | None:
    if not date:
        return None
    return int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp() * 1000)


def _data_venues(settings: Settings, names: list[str] | None = None, auth: bool = False):
    from .venues.ccxt_venue import CcxtVenue, credentials_from_env

    out = {}
    for vs in settings.venues:
        if names and vs.name not in names:
            continue
        creds = credentials_from_env(vs.name) if auth else None
        out[vs.name] = CcxtVenue(vs.name, vs.ccxt_id, vs.quotes, creds, vs.options, kinds=vs.kinds)
    return out


def _check_venue(venue, has_creds: bool, step) -> None:
    from .models import Kind

    insts = (
        step(
            "markets",
            venue.load_instruments,
            lambda r: (
                f"{sum(i.kind is Kind.PERP for i in r)} perps, "
                f"{sum(i.kind is Kind.SPOT for i in r)} spot markets"
            ),
        )
        or []
    )
    perps = [i for i in insts if i.kind is Kind.PERP][:3]
    spots = [i for i in insts if i.kind is Kind.SPOT][:3]
    if perps:
        step(
            "funding",
            lambda: venue.fetch_funding(perps),
            lambda r: ", ".join(
                f"{x.instrument.asset} {x.rate:+.6f}/{x.interval_hours:g}h" for x in r.values()
            ),
        )
    if perps or spots:
        sample = (perps + spots)[:4]
        step(
            "quotes",
            lambda: venue.fetch_quotes(sample),
            lambda r: ", ".join(
                f"{k.split(':', 1)[1]} {q.bid:.6g}/{q.ask:.6g}" for k, q in r.items()
            ),
        )
    if not has_creds:
        print("  skip  account checks (no API credentials in the environment)")
        return
    step(
        "balance",
        venue.ex.fetch_balance,
        lambda r: (
            ", ".join(f"{k} {v:,.6g}" for k, v in list((r.get("total") or {}).items()) if v)
            or "empty"
        ),
    )
    if perps:
        step(
            "positions",
            venue.fetch_perp_positions,
            lambda r: (
                ", ".join(f"{p.instrument.symbol} {p.qty:+.6g}" for p in r.values()) or "none"
            ),
        )


def cmd_check(args, settings: Settings) -> None:
    """Connectivity, market data and (if keys are set) account access, per venue."""
    from .venues.ccxt_venue import credentials_from_env

    failures = 0

    def step(label, fn, describe):
        nonlocal failures
        try:
            result = fn()
            print(f"  ok    {label}: {describe(result)}")
            return result
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {label}: {type(exc).__name__}: {str(exc)[:300]}")
            return None

    for name, venue in _data_venues(settings, auth=True).items():
        creds = credentials_from_env(name)
        print(f"{name}  (ccxt id: {venue.ex.id}; credentials: {sorted(creds) or 'none'})")
        _check_venue(venue, bool(creds), step)
    if failures:
        sys.exit(f"{failures} check(s) failed")


def cmd_status(args, settings: Settings) -> None:
    """Print positions and P&L from the engine's state file (works while it runs)."""
    from pathlib import Path

    from .state import load_json, position_from_dict

    path = Path(_state_dir(settings, args.live)) / "engine.json"
    data = load_json(path)
    if not data:
        sys.exit(f"no state at {path}; has `fcap {'live' if args.live else 'paper'}` run yet?")
    open_ = [position_from_dict(d) for d in data.get("positions", [])]
    closed = [position_from_dict(d) for d in data.get("closed", [])]
    print(f"state: {path}")
    print(f"open pairs: {len(open_)}")
    for p in open_:
        age_h = (datetime.now(UTC).timestamp() * 1000 - p.opened_ms) / 3_600_000
        print(
            f"  {p.id}  {p.candidate.describe()}  qty={p.qty:.6g} "
            f"notional=${p.entry_notional:,.0f}  age={age_h:.1f}h  "
            f"funding=${p.funding_usd:,.2f}  fees=${p.entry_fees_usd + p.exit_fees_usd:,.2f}"
        )
        for note in p.notes[-3:]:
            print(f"      - {note}")
    print(f"closed pairs: {len(closed)}")
    for p in closed[-args.last :]:
        print(
            f"  {p.id}  {p.candidate.describe()}  net=${p.net_pnl_usd:,.2f} "
            f"(funding ${p.funding_usd:,.2f}, fees ${p.entry_fees_usd + p.exit_fees_usd:,.2f}, "
            f"price ${p.realized_price_usd:,.2f})  {p.notes[-1] if p.notes else ''}"
        )
    print(f"realised net P&L: ${sum(p.net_pnl_usd for p in closed):,.2f}")


def cmd_scan(args, settings: Settings) -> None:
    from .data import HistoryStore
    from .scanner import format_rows, scan

    venues = _data_venues(settings)
    store = HistoryStore(settings.data_dir)
    rows = scan(venues, settings.strategy, settings.costs, settings.risk, settings.portfolio, store)
    print(format_rows(rows, args.top, only_tradeable=not args.all))


def cmd_fetch(args, settings: Settings) -> None:
    from .data import HistoryStore, download_funding_history, save_universe, select_perps

    store = HistoryStore(settings.data_dir)
    since = _ms(args.since) or int(
        (datetime.now(UTC) - timedelta(days=args.days)).timestamp() * 1000
    )
    assets = set(args.assets.split(",")) if args.assets else None
    names = args.venues.split(",") if args.venues else None
    venue_instruments, venue_perps = {}, {}
    for name, venue in _data_venues(settings, names).items():
        try:
            insts = venue.load_instruments()
        except Exception as exc:
            log.error("%s: cannot load markets (%s); skipping", name, exc)
            continue
        perps = select_perps(insts, venue, assets, args.min_volume, args.max_markets)
        venue_instruments[name], venue_perps[name] = insts, perps
        log.info(
            "%s: %d spot markets, downloading %d perps",
            name,
            sum(i.kind.value == "spot" for i in insts),
            len(perps),
        )
        added = download_funding_history(venue, store, perps, since, _ms(args.until))
        log.info("%s: %d markets, %d new settlements", name, len(added), sum(added.values()))
    save_universe(store, venue_instruments, venue_perps)


def _backtester(args, settings: Settings):
    import dataclasses

    from .backtest import Backtester
    from .data import HistoryStore

    instruments, series = HistoryStore(args.data or settings.data_dir).load_all()
    if not series:
        sys.exit("no funding history found; run `fcap fetch` first")
    cfg = dataclasses.replace(
        settings.backtest,
        start_ms=_ms(args.start) or settings.backtest.start_ms,
        end_ms=_ms(args.end) or settings.backtest.end_ms,
        capital_usd=settings.portfolio.capital_usd,
        max_positions=settings.portfolio.max_positions,
        max_positions_per_asset=settings.portfolio.max_positions_per_asset,
        min_position_usd=settings.portfolio.min_position_usd,
    )
    return Backtester(instruments, series, settings.strategy, settings.costs, cfg)


def cmd_backtest(args, settings: Settings) -> None:
    res = _backtester(args, settings).run()
    print(res.report())
    if args.trades:
        res.trades.to_csv(args.trades, index=False)
        print(f"trades written to {args.trades}")


def cmd_sweep(args, settings: Settings) -> None:
    import pandas as pd

    from .backtest import sweep

    grid = {
        "entry_apr": [0.10, 0.20, 0.40],
        "exit_apr": [-0.05, 0.0],
        "horizon_hours": [72, 168, 336],
        "forecast.reversion_halflife_hours": [24, 72, 240],
        "forecast.anchor_halflife_hours": [None, 720],
    }
    df = sweep(_backtester(args, settings), grid, split=args.split)
    with pd.option_context("display.width", 200, "display.max_rows", 200):
        print(df.head(args.top).to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print("\nChoose by test_apr_pct stability, not by the best train row.")


def _state_dir(settings: Settings, live: bool) -> str:
    """Paper and live keep separate state so paper positions are never mistaken for real."""
    from pathlib import Path

    return str(Path(settings.portfolio.state_dir) / ("live" if live else "paper"))


def _engine(settings: Settings, live: bool):
    import dataclasses

    from .data import HistoryStore
    from .engine import Engine
    from .execution import PairExecutor
    from .venues.paper import PaperVenue

    data = _data_venues(settings, auth=live)
    if live:
        venues = data
    else:
        lev = {vs.name: vs.paper_leverage for vs in settings.venues}
        venues = {
            n: PaperVenue(v, settings.costs.fees.get(n), leverage=lev.get(n, 2.0))
            for n, v in data.items()
        }
    executor = PairExecutor(venues, settings.execution, settings.costs.maker_venues)
    return Engine(
        venues,
        settings.strategy,
        settings.costs,
        executor,
        settings.risk,
        dataclasses.replace(settings.portfolio, state_dir=_state_dir(settings, live)),
        store=HistoryStore(settings.data_dir),
    )


def cmd_paper(args, settings: Settings) -> None:
    _engine(settings, live=False).run(max_steps=args.steps)


def cmd_live(args, settings: Settings) -> None:
    if settings.mode != "live" or not args.i_understand_this_trades_real_money:
        sys.exit(
            "live trading needs `mode: live` in the config AND "
            "--i-understand-this-trades-real-money. Paper trade first."
        )
    _engine(settings, live=True).run(max_steps=args.steps)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="fcap", description=__doc__)
    p.add_argument("-c", "--config", default=None, help="YAML config (see config.example.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--env-file", default=".env", help="API keys file (default: ./.env)")
    p.add_argument("--log-file", help="also append logs to this file")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check", help="test connectivity, market data and API keys per venue")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("status", help="show positions and P&L from the engine state file")
    s.add_argument("--last", type=int, default=20, help="closed pairs to list")
    s.add_argument("--live", action="store_true", help="show the live engine (default: paper)")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("scan", help="rank live opportunities across venues")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--all", action="store_true", help="include rows failing liquidity filters")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("fetch", help="download historical funding settlements")
    s.add_argument("--venues", help="comma list (default: all configured)")
    s.add_argument("--days", type=int, default=365)
    s.add_argument("--since")
    s.add_argument("--until")
    s.add_argument("--assets", help="comma list of canonical assets, e.g. BTC,ETH,SOL")
    s.add_argument("--min-volume", type=float, default=2_000_000, help="24h USD volume filter")
    s.add_argument("--max-markets", type=int, default=150, help="top N perps by volume per venue")
    s.set_defaults(fn=cmd_fetch)

    for name, fn, helptext in (
        ("backtest", cmd_backtest, "simulate the strategy on downloaded history"),
        ("sweep", cmd_sweep, "parameter grid with walk-forward split"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--data", help="history directory (default: data_dir from config)")
        s.add_argument("--start")
        s.add_argument("--end")
        if name == "backtest":
            s.add_argument("--trades", help="write trade log CSV here")
        else:
            s.add_argument("--split", type=float, default=0.5)
            s.add_argument("--top", type=int, default=30)
        s.set_defaults(fn=fn)

    s = sub.add_parser("paper", help="run the engine with simulated fills on live data")
    s.add_argument("--steps", type=int, default=None)
    s.set_defaults(fn=cmd_paper)

    s = sub.add_parser("live", help="run the engine with real orders")
    s.add_argument("--steps", type=int, default=None)
    s.add_argument("--i-understand-this-trades-real-money", action="store_true")
    s.set_defaults(fn=cmd_live)

    args = p.parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("ccxt").setLevel(logging.WARNING)  # ccxt is chatty at DEBUG
    loaded = load_env_file(args.env_file)
    if loaded:
        log.debug("loaded %d variables from %s", loaded, args.env_file)
    args.fn(args, load_settings(args.config))


if __name__ == "__main__":
    main()
