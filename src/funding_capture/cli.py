"""Command line: fcap scan | fetch | backtest | sweep | paper | live."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

from .config import Settings, load_settings

log = logging.getLogger("funding_capture")


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
        out[vs.name] = CcxtVenue(vs.name, vs.ccxt_id, vs.quotes, creds, vs.options)
    return out


def cmd_scan(args, settings: Settings) -> None:
    from .data import HistoryStore
    from .scanner import format_rows, scan

    venues = _data_venues(settings)
    store = HistoryStore(settings.data_dir)
    rows = scan(venues, settings.strategy, settings.costs, settings.risk, settings.portfolio, store)
    print(format_rows(rows, args.top, only_tradeable=not args.all))


def cmd_fetch(args, settings: Settings) -> None:
    from .data import HistoryStore, download_funding_history

    store = HistoryStore(settings.data_dir)
    since = _ms(args.since) or int(
        (datetime.now(UTC) - timedelta(days=args.days)).timestamp() * 1000
    )
    assets = set(args.assets.split(",")) if args.assets else None
    names = args.venues.split(",") if args.venues else None
    for name, venue in _data_venues(settings, names).items():
        log.info("downloading %s", name)
        added = download_funding_history(
            venue, store, since, _ms(args.until), assets, args.min_volume, args.max_markets
        )
        log.info("%s: %d markets, %d new settlements", name, len(added), sum(added.values()))


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


def _engine(settings: Settings, live: bool):
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
    executor = PairExecutor(venues, settings.execution)
    return Engine(
        venues,
        settings.strategy,
        settings.costs,
        executor,
        settings.risk,
        settings.portfolio,
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
    sub = p.add_subparsers(dest="cmd", required=True)

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
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args.fn(args, load_settings(args.config))


if __name__ == "__main__":
    main()
