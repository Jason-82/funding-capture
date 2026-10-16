"""Command line: fcap check | keys | markets | scan | record | import-funding | fetch | backtest
| sweep | paper | live | status | forget."""

from __future__ import annotations

import argparse
import contextlib
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
    for line in p.read_text(encoding="utf-8-sig").splitlines():  # -sig: Notepad BOM
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
    from .venues.coinbase import CoinbaseVenue

    out = {}
    for vs in settings.venues:
        if names and vs.name not in names:
            continue
        creds = credentials_from_env(vs.name) if auth else None
        klass = CoinbaseVenue if (vs.ccxt_id or vs.name) == "coinbase" else CcxtVenue
        out[vs.name] = klass(
            vs.name,
            vs.ccxt_id,
            vs.quotes,
            creds,
            vs.options,
            kinds=vs.kinds,
            dead_man_seconds=vs.dead_man_seconds if auth else 0,
        )
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
                f"{k.split(':', 1)[1]} {q.bid:.10g}/{q.ask:.10g}" for k, q in r.items()
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
    if spots and venue.ex.has.get("fetchTradingFee"):
        step(
            "your spot fee",
            lambda: venue.ex.fetch_trading_fee(spots[0].symbol),
            lambda r: (
                f"spot_maker: {float(r.get('maker') or 0):.4f}  "
                f"spot_taker: {float(r.get('taker') or 0):.4f}"
                f"  (fractions; paste under fees: {venue.name}:)"
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
        buf = venue.margin_buffer()
        if buf is not None:
            print(f"  ok    margin buffer: {buf:.1%} from liquidation")


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

    for vs in settings.venues:
        name = vs.name
        try:
            creds = credentials_from_env(name)
            venue = _data_venues(settings, [name], auth=True)[name]
        except Exception as exc:
            failures += 1
            print(f"{name}\n  FAIL  credentials: {exc}")
            continue
        print(f"{name}  (ccxt id: {venue.ex.id}; credentials: {sorted(creds) or 'none'})")
        _check_venue(venue, bool(creds), step)
    if failures:
        sys.exit(f"{failures} check(s) failed")


def _liveness(path, live: bool, settings: Settings) -> str:
    """Whether the engine runs, from its lock (an engine holds it while it runs; the OS
    drops it when the process ends), and how long ago it last saved."""
    from .state import StateLocked, lock_state_dir

    which = "live" if live else "paper"
    age_min = (datetime.now(UTC).timestamp() - path.stat().st_mtime) / 60
    try:
        with lock_state_dir(path.parent):
            running = False
    except StateLocked:
        running = True
    if not running:
        return f"WARNING: the {which} engine is NOT running (last saved {age_min:.0f} min ago)"
    line = f"engine: running, last saved {age_min:.0f} min ago"
    if age_min > max(10.0, 3 * settings.portfolio.loop_seconds / 60):
        line += (
            "\nNOTE: it is running but has not saved for a while: busy with an order, or "
            "unable to write engine.json (look for CRITICAL in its window). Leave the "
            "exchanges alone."
        )
    return line


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
    print(_liveness(path, args.live, settings))
    if data.get("entries_halted"):
        print(f"ENTRIES HALTED: {data['entries_halted']}")
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
    realised = sum(p.net_pnl_usd for p in closed)
    running = sum(p.funding_usd - p.borrow_usd - p.entry_fees_usd - p.exit_fees_usd for p in open_)
    print(f"realised net P&L (closed pairs): ${realised:,.2f}")
    print(f"open pairs so far (funding minus fees paid, before exit fees): ${running:,.2f}")
    started = data.get("started_ms")
    capital = data.get("capital_usd") or settings.portfolio.capital_usd
    if started:
        days = (datetime.now(UTC).timestamp() * 1000 - started) / 86_400_000
        # open pairs are counted as if closed now: minus what closing them will cost
        exit_est = sum(
            settings.costs.round_trip_cost(p.candidate, p.long_entry, p.short_entry)
            / 2
            * p.entry_notional
            for p in open_
        )
        if open_:
            print(f"estimated cost to close the open pairs: ${exit_est:,.2f} (counted below)")
        total = realised + running - exit_est
        print(f"first started {days:.1f} days ago, on ${capital:,.0f} of capital")
        if days >= 1:
            print(
                f"annualised return on capital so far: {total / capital * 365 / days:+.1%}"
                "  (noisy until a few weeks have passed)"
            )


def cmd_forget(args, settings: Settings) -> None:
    """Drop a pair you closed by hand from the engine's record. Otherwise, on the next
    start the bot sees its legs gone and may sell coins of that kind from the account,
    including coins you own. Refuses while the engine runs."""
    from pathlib import Path

    from .state import (
        StateLocked,
        load_json,
        lock_state_dir,
        position_from_dict,
        position_to_dict,
        save_json,
    )

    d = Path(_state_dir(settings, args.live))
    path = d / "engine.json"
    try:
        with lock_state_dir(d):
            data = load_json(path) or {}
            positions = data.get("positions", [])
            match = [p for p in positions if p.get("id") == args.pair_id]
            if not match:
                ids = ", ".join(p.get("id", "?") for p in positions) or "none"
                sys.exit(f"no open pair {args.pair_id} in {path} (open pairs: {ids})")
            pos = position_from_dict(match[0])
            pos.closed_ms = int(datetime.now(UTC).timestamp() * 1000)
            pos.notes.append("closed by hand; removed with `fcap forget`")
            data["positions"] = [p for p in positions if p.get("id") != args.pair_id]
            data["closed"] = [*data.get("closed", []), position_to_dict(pos)]
            save_json(path, data)
    except StateLocked:
        sys.exit("the engine is running: stop it with QUIT first, then run forget again")
    print(f"removed {args.pair_id} from {path}; the bot no longer manages it")


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
    for name, perps in venue_perps.items():  # keep markets with history from record/import
        have = {p.key for p in perps}
        perps.extend(
            i
            for i in store.load_instruments(name)
            if i.kind.value == "perp" and i.key not in have and store.load_funding(i)
        )
    save_universe(store, venue_instruments, venue_perps)


def _write_env(path: str, values: dict[str, str]) -> None:
    """Set KEY=VALUE lines in a .env file (replacing old ones), readable only by you."""
    import os
    from pathlib import Path

    p = Path(path)
    keep = []
    if p.exists():
        keep = [
            line
            for line in p.read_text(encoding="utf-8-sig").splitlines()
            if line.split("=", 1)[0].removeprefix("export ").strip() not in values
        ]
    keep += [f"{k}={v}" for k, v in values.items()]
    p.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
    os.chmod(p, 0o600)


def _ask(prompt: str, hidden: bool = False) -> str:
    """Prompt for a value. Hidden input uses getpass in a real console; Git Bash (mintty)
    has none and getpass can hang there, so it falls back to a visible line read."""
    import getpass
    import os

    git_bash = bool(os.environ.get("MSYSTEM")) or not sys.stdin.isatty()
    if hidden and not git_bash:
        return getpass.getpass(prompt + "(input hidden) ").strip()
    if hidden:
        prompt += "(paste, then Enter; it will be visible, so run `clear` afterwards) "
    print(prompt, end="", flush=True)
    return sys.stdin.readline().strip()


def cmd_keys(args, settings: Settings) -> None:
    """Store API keys for a venue in .env (Coinbase: from the downloaded key file)."""
    import json
    import os
    from pathlib import Path

    venue = args.venue.lower()
    if venue == "coinbase":
        if not args.json:
            sys.exit(
                "Coinbase: pass the downloaded key file, e.g. --json ~/Downloads/cdp_api_key.json"
            )
        data = json.loads(Path(args.json).expanduser().read_text(encoding="utf-8-sig"))
        name = data.get("name") or data.get("id")
        secret = data.get("privateKey")
        if not name or not secret:
            sys.exit(f"{args.json} has no name/id and privateKey; is it the CDP API key file?")
        values = {"COINBASE_API_KEY": name}
        if "BEGIN" in secret:  # ECDSA: keep the multi-line PEM in its own file
            folder = Path(os.environ.get("FCAP_KEY_DIR", "~/.fcap")).expanduser()
            folder.mkdir(mode=0o700, exist_ok=True)
            pem = folder / "coinbase.pem"
            body = secret if secret.endswith("\n") else secret + "\n"
            pem.write_text(body, encoding="ascii", newline="\n")
            os.chmod(pem, 0o600)
            values["COINBASE_SECRET_FILE"] = str(pem)
        else:  # Ed25519: a single base64 line
            values["COINBASE_SECRET"] = secret
    else:
        prefix = venue.upper()
        key = _ask(f"{venue} API key: ")
        secret = _ask(f"{venue} private key: ", hidden=True)
        if not key or not secret:
            sys.exit("nothing entered; .env unchanged")
        values = {f"{prefix}_API_KEY": key, f"{prefix}_SECRET": secret}
    _write_env(args.env_file, values)
    # Windows has no owner-only mode bits; the files rely on the user profile's ACL there
    where = "" if os.name == "nt" else " (permissions: owner only)"
    print(f"saved {', '.join(values)} to {args.env_file}{where}")
    print("next: fcap -c config.yaml check")


def cmd_markets(args, settings: Settings) -> None:
    """List a venue's markets as the bot sees them (symbol, asset, size per contract)."""
    from .models import Kind

    venue = _data_venues(settings, [args.venue])[args.venue]
    insts = sorted(venue.load_instruments(), key=lambda i: (i.kind.value, i.asset))
    if args.kind:
        insts = [i for i in insts if i.kind is Kind(args.kind)]
    marks = {}
    perps = [i for i in insts if i.is_perp]
    if perps:
        marks = {k: s.mark_price for k, s in venue.fetch_funding(perps).items()}
    print(f"{'symbol':<24} {'kind':<5} {'asset':<10} {'units/contract':>15} {'$/contract':>11}")
    for i in insts:
        unit = i.multiplier * (i.contract_size if i.is_perp else 1.0)
        mark = marks.get(i.key)
        value = f"{unit * mark:>11,.2f}" if mark else f"{'':>11}"
        print(f"{i.symbol:<24} {i.kind.value:<5} {i.asset:<10} {unit:>15,.6g} {value}")


def cmd_record(args, settings: Settings) -> None:
    """Log published funding forward into the history store (for venues with no history
    API, e.g. Coinbase US perps). The last reading before each settlement is kept."""
    import time

    from .data import HistoryStore, save_universe
    from .models import Kind

    store = HistoryStore(settings.data_dir)
    venues = _data_venues(settings, args.venues.split(",") if args.venues else None)
    insts = {n: v.load_instruments() for n, v in venues.items()}
    perps = {n: [i for i in xs if i.kind is Kind.PERP] for n, xs in insts.items()}
    save_universe(store, insts, perps)
    while True:
        n = 0
        for name, venue in venues.items():
            if not perps[name]:
                continue
            try:
                snaps = venue.fetch_funding(perps[name])
            except Exception as exc:
                log.warning("%s: funding fetch failed: %s", name, exc)
                continue
            for snap in snaps.values():
                if snap.next_funding_ts:
                    store.append_funding(snap.instrument, [(int(snap.next_funding_ts), snap.rate)])
                    n += 1
        log.info("recorded %d funding readings", n)
        if args.once:
            return
        time.sleep(args.interval)


def cmd_import(args, settings: Settings) -> None:
    from .data import HistoryStore, read_funding_csv, save_universe
    from .models import Kind

    store = HistoryStore(settings.data_dir)
    insts = {i.symbol: i for i in store.load_instruments(args.venue)}
    if args.symbol not in insts:  # first time: learn the venue's markets
        venue = _data_venues(settings, [args.venue])[args.venue]
        loaded = venue.load_instruments()
        save_universe(
            store, {args.venue: loaded}, {args.venue: [i for i in loaded if i.kind is Kind.PERP]}
        )
        insts = {i.symbol: i for i in loaded}
    if args.symbol not in insts:
        sys.exit(f"{args.symbol} is not a market on {args.venue}")
    rows = read_funding_csv(args.file, args.time_col, args.rate_col, args.percent)
    added = store.append_funding(insts[args.symbol], rows)
    print(f"{len(rows)} rows read, {added} new settlements stored for {args.venue} {args.symbol}")
    if rows:
        print(f"first {rows[0]}, last {rows[-1]}; check that rates look like fractions per hour")


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
        max_position_usd=settings.portfolio.max_position_usd,
    )
    return Backtester(instruments, series, settings.strategy, settings.costs, cfg)


def cmd_backtest(args, settings: Settings) -> None:
    res = _backtester(args, settings).run()
    print(res.report())
    if args.trades:
        res.trades.to_csv(args.trades, index=False)
        print(f"trades written to {args.trades}")


def _mark_current(df, grid: dict[str, list]):
    """Rank order kept, index reset, and a ``yours`` column with * on the row whose values
    are the first of each grid list (the config's own settings)."""
    import pandas as pd

    df = df.reset_index(drop=True)

    def same(a, b) -> bool:  # pandas stores a None setting as NaN
        return bool(pd.isna(a)) if b is None else a == b

    current = {k: v[0] for k, v in grid.items()}
    marks = [
        "*" if all(same(r[k], v) for k, v in current.items()) else "" for _, r in df.iterrows()
    ]
    df.insert(0, "yours", marks)
    return df


def cmd_sweep(args, settings: Settings) -> None:
    import pandas as pd

    from .backtest import sweep

    st = settings.strategy

    def around(current, *others):  # your setting plus alternatives, no repeats
        return list(dict.fromkeys([current, *others]))

    grid = {
        "entry_apr": around(st.entry_apr, 0.10, 0.20, 0.30),
        "exit_apr": around(st.exit_apr, -0.05, 0.0),
        "horizon_hours": around(st.horizon_hours, 336, 720, 1440),
        "forecast.reversion_halflife_hours": around(st.forecast.reversion_halflife_hours, 72, 240),
        "forecast.anchor_halflife_hours": around(st.forecast.anchor_halflife_hours, None, 720),
    }
    df = _mark_current(sweep(_backtester(args, settings), grid, split=args.split), grid)
    with pd.option_context("display.width", 200, "display.max_rows", 200):
        lines = df.to_string(index=False, float_format=lambda x: f"{x:,.2f}").splitlines()
    print("\n".join(lines[: args.top + 1]))
    mine = df.index[df["yours"] == "*"]
    if len(mine) and mine[0] >= args.top:  # show your row even if it ranks low, aligned
        print("...")
        print(f"{lines[mine[0] + 1]}   (rank {mine[0] + 1} of {len(df)})")
    print(
        "\nEach row is one combination: train_* is its result on the first part of the data, "
        "test_* on the rest. * marks your current settings. Prefer rows whose test_apr_pct "
        "is good and close to train_apr_pct; don't just take the top row."
    )


def _state_dir(settings: Settings, live: bool) -> str:
    """Paper and live keep separate state so paper positions are never mistaken for real."""
    from pathlib import Path

    return str(Path(settings.portfolio.state_dir) / ("live" if live else "paper"))


def _engine(settings: Settings, live: bool):
    import dataclasses
    from pathlib import Path

    old = Path(settings.portfolio.state_dir) / "engine.json"
    new = Path(_state_dir(settings, live)) / "engine.json"
    if old.exists() and not new.exists():
        sys.exit(
            f"found {old} from an older version, which kept paper and live state together. "
            f"Move it to {new} if it belongs to {'live' if live else 'paper'} trading (or "
            "delete it if it was paper), then start again."
        )

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


def _run_engine(settings: Settings, live: bool, steps: int | None) -> None:
    from .state import StateLocked

    try:
        _engine(settings, live=live).run(max_steps=steps)
    except StateLocked as exc:
        d = _state_dir(settings, live).replace("\\", "/")
        sys.exit(
            f"{exc}: its window ({'LIVE' if live else 'PAPER'}) is still open. Use that one, "
            f"or stop it with `touch {d}/QUIT` and wait for its $ prompt."
        )


def cmd_paper(args, settings: Settings) -> None:
    _run_engine(settings, live=False, steps=args.steps)


def cmd_live(args, settings: Settings) -> None:
    if settings.mode != "live" or not args.i_understand_this_trades_real_money:
        sys.exit(
            "live trading needs `mode: live` in the config AND "
            "--i-understand-this-trades-real-money. Paper trade first."
        )
    _run_engine(settings, live=True, steps=args.steps)


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

    s = sub.add_parser("forget", help="drop a pair you closed by hand from the record")
    s.add_argument("pair_id", help="the pair's id as `status` shows it, e.g. P1760000000-1")
    s.add_argument("--live", action="store_true", help="the live engine (default: paper)")
    s.set_defaults(fn=cmd_forget)

    s = sub.add_parser("scan", help="rank live opportunities across venues")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--all", action="store_true", help="include rows failing liquidity filters")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("keys", help="store a venue's API keys in .env")
    s.add_argument("venue", help="coinbase or kraken (or another configured venue)")
    s.add_argument("--json", help="Coinbase: path of the downloaded CDP key file")
    s.set_defaults(fn=cmd_keys)

    s = sub.add_parser("markets", help="list a venue's markets and contract sizes")
    s.add_argument("--venue", required=True)
    s.add_argument("--kind", choices=["spot", "perp"])
    s.set_defaults(fn=cmd_markets)

    s = sub.add_parser("record", help="log funding forward (venues without a history API)")
    s.add_argument("--venues", help="comma list (default: all configured)")
    s.add_argument("--interval", type=float, default=600, help="seconds between readings")
    s.add_argument("--once", action="store_true")
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("import-funding", help="load a funding-rate CSV from an exchange site")
    s.add_argument("--venue", required=True)
    s.add_argument("--symbol", required=True, help="venue symbol, e.g. BIP-20DEC30-CDE")
    s.add_argument("--file", required=True)
    s.add_argument("--time-col")
    s.add_argument("--rate-col")
    s.add_argument("--percent", action="store_true", help="rates in the file are percents")
    s.set_defaults(fn=cmd_import)

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
    for stream in (sys.stdout, sys.stderr):
        # Git Bash hands Windows Python a pipe, which it block-buffers and encodes as
        # cp1252: output would arrive in bursts and non-ASCII text would raise.
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
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
