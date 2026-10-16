"""Venue adapter over ccxt.

Market data paths (load_instruments, fetch_funding, fetch_quotes, history) are what the
scanner/backtest/paper engine use. The order/position paths are written against ccxt's
unified API but venue quirks (position mode, margin mode, fee currency, min notional,
subaccount routing) need checking on each venue with tiny size before real use.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid

from ..models import (
    Fill,
    FundingSnapshot,
    Instrument,
    Kind,
    OrderState,
    PerpPositionInfo,
    Quote,
)
from ..rates import DEFAULT_INTERVAL_HOURS, parse_interval_hours
from ..symbols import canonical_asset
from .base import Venue

log = logging.getLogger(__name__)

# Per-venue credential fields we read from the environment: ccxt field -> ENV suffix.
_CRED_FIELDS = {
    "apiKey": "API_KEY",
    "secret": "SECRET",
    "password": "PASSWORD",
    "walletAddress": "WALLET_ADDRESS",
    "privateKey": "PRIVATE_KEY",
}


def credentials_from_env(venue: str) -> dict[str, str]:
    """``KRAKEN_API_KEY`` etc. -> ccxt constructor fields. Never put keys in config files.

    Each value may instead come from a file named by ``<NAME>_FILE`` (handy for multi-line
    PEM keys such as Coinbase's), and a literal ``\\n`` in a value is turned into a newline.
    """
    prefix = venue.upper()
    out: dict[str, str] = {}
    for field, suffix in _CRED_FIELDS.items():
        name = f"{prefix}_{suffix}"
        value = os.environ.get(name)
        if not value and os.environ.get(f"{name}_FILE"):
            path = os.path.expanduser(os.environ[f"{name}_FILE"])
            try:
                with open(path, encoding="utf-8") as f:
                    value = f.read().strip()
            except OSError as exc:
                raise ValueError(f"{name}_FILE: cannot read {path}: {exc.strerror}") from None
        if value:
            out[field] = value.replace("\\n", "\n")
    return out


# Options we always want for some ccxt exchanges (user options override these).
_DEFAULT_OPTIONS: dict[str, dict] = {
    # ccxt otherwise approves and attaches its own builder fee (+0.01% per fill), binds the
    # account to its referral code, and loads HIP-3 (builder-deployed) markets.
    "hyperliquid": {
        "builderFee": False,
        "refSet": True,
        "fetchMarkets": {"types": ["spot", "swap"]},
    },
    # ccxt otherwise attaches a 0.10% integrator fee per fill on non-standard accounts.
    "lighter": {"builderFee": False},
}

# Extra params on every order. Kraken: take the fee in the quote currency so the coin
# quantity bought/sold matches the hedge exactly (default for sells is fee-in-coin).
_ORDER_PARAMS: dict[str, dict] = {"kraken": {"oflags": "fciq"}}

# Venues whose ccxt funding rate is quoted per N hours although they settle hourly.
# Lighter's /funding-rates normalises every venue to an 8h-equivalent fraction.
_RATE_QUOTED_PER_HOURS: dict[str, float] = {"lighter": 8.0}

# Funding-history page sizes for venues whose ccxt pagination is deterministic (8h steps).
_HISTORY_PAGE: dict[str, int] = {"binance": 1000, "okx": 100, "gate": 1000, "bingx": 1000}

# Up to this many instruments without a usable ticker bid/ask get an order-book quote.
_BOOK_FALLBACK_MAX = 20


def ex_id_of(exchange) -> str:
    return getattr(exchange, "id", "")


def merged_opt(exchange, key: str):
    return (getattr(exchange, "options", None) or {}).get(key)


class CcxtVenue(Venue):
    def __init__(
        self,
        name: str,
        ccxt_id: str | None = None,
        quotes: tuple[str, ...] = ("USDT", "USDC"),
        credentials: dict | None = None,
        options: dict | None = None,
        aliases: dict[str, str] | None = None,
        exchange=None,
        kinds: tuple[str, ...] = ("spot", "perp"),
        dead_man_seconds: int = 0,
    ):
        self.name = name
        self.quotes = tuple(quotes)  # accepted quote/settle currencies, most preferred first
        self.kinds = {Kind(k) for k in kinds}
        self.aliases = aliases
        if exchange is None:
            import ccxt

            ex_id = ccxt_id or name
            merged = {**_DEFAULT_OPTIONS.get(ex_id, {}), **(options or {})}
            exchange = getattr(ccxt, ex_id)(
                {"enableRateLimit": True, **(credentials or {}), "options": merged}
            )
        self.ex = exchange
        if ex_id_of(exchange) == "hyperliquid" and not merged_opt(exchange, "builderFee"):
            # with an API (agent) wallet the zero-fee approval fails and ccxt would retry it
            # before every order; there is nothing to approve, so skip it
            exchange.handle_builder_fee_approval = lambda *a, **k: True
        self.dead_man_seconds = dead_man_seconds
        has = getattr(exchange, "has", None) or {}
        # Without fetchOrder we cannot learn what an order filled (ccxt lighter): such a
        # venue is for scanning/backtesting only. Without fetchFundingHistory the engine
        # estimates funding from published rates.
        self.can_trade = bool(has.get("fetchOrder"))
        self.reports_funding = bool(has.get("fetchFundingHistory"))
        self._by_symbol: dict[str, Instrument] = {}
        self._intervals: dict[str, float] = {}

    # ------------------------------------------------------------------ market data
    def load_instruments(self) -> list[Instrument]:
        if self._by_symbol:
            return list(self._by_symbol.values())
        markets = self.ex.load_markets()
        best_spot: dict[str, Instrument] = {}  # one spot market per asset: best-ranked quote
        for sym, m in markets.items():
            if m.get("expiry"):
                continue
            # Kraken omits `status` on ~13% of pairs and ccxt then marks them inactive
            unknown = self.ex.id == "kraken" and (m.get("info") or {}).get("status") is None
            if m.get("active") is False and not unknown:
                continue
            if m.get("swap") and m.get("linear") and m.get("settle") in self.quotes:
                kind, quote = Kind.PERP, m["settle"]
                contract_size = float(m.get("contractSize") or 1.0)
            elif m.get("spot") and m.get("quote") in self.quotes:
                kind, quote, contract_size = Kind.SPOT, m["quote"], 1.0
            else:
                continue
            if kind not in self.kinds:
                continue
            base, raw = m["base"], m.get("baseName")
            if raw and raw[:1] == "k" and raw[1:].upper() == base[1:]:
                base = raw  # Hyperliquid's kPEPE (1000 PEPE) arrives as base "KPEPE"
            asset, mult = canonical_asset(base, self.aliases)
            inst = Instrument(self.name, sym, asset, kind, quote, mult, contract_size)
            if kind is Kind.PERP:
                self._by_symbol[sym] = inst
                continue
            cur = best_spot.get(asset)
            if cur is None or self.quotes.index(quote) < self.quotes.index(cur.quote):
                best_spot[asset] = inst
        self._by_symbol.update({i.symbol: i for i in best_spot.values()})
        self._load_intervals()
        return list(self._by_symbol.values())

    def _load_intervals(self) -> None:
        if not self.ex.has.get("fetchFundingIntervals"):
            return
        try:
            for sym, info in (self.ex.fetch_funding_intervals() or {}).items():
                hours = parse_interval_hours(info.get("interval"))
                if hours:
                    self._intervals[sym] = hours
        except Exception as exc:
            log.debug("%s: fetch_funding_intervals failed: %s", self.name, exc)

    def _perps(self, instruments: list[Instrument] | None) -> list[Instrument]:
        self.load_instruments()
        insts = instruments if instruments is not None else list(self._by_symbol.values())
        return [i for i in insts if i.kind is Kind.PERP]

    def fetch_funding(
        self, instruments: list[Instrument] | None = None
    ) -> dict[str, FundingSnapshot]:
        perps = {i.symbol: i for i in self._perps(instruments)}
        if not perps:
            return {}
        now = int(time.time() * 1000)
        try:
            raw = self.ex.fetch_funding_rates(list(perps))
        except Exception as exc:
            if len(perps) > 30:
                raise
            log.debug("%s: bulk funding failed (%s); per-symbol fallback", self.name, exc)
            raw = {s: self.ex.fetch_funding_rate(s) for s in perps}
        out: dict[str, FundingSnapshot] = {}
        for sym, fr in raw.items():
            inst = perps.get(sym)
            rate = fr.get("fundingRate")
            if inst is None or rate is None:
                continue
            interval = (
                parse_interval_hours(fr.get("interval"))
                or self._intervals.get(sym)
                or DEFAULT_INTERVAL_HOURS.get(
                    self.ex.id, DEFAULT_INTERVAL_HOURS.get(self.name, 8.0)
                )
            )
            rate = float(rate)
            next_ts = fr.get("fundingTimestamp") or fr.get("nextFundingTimestamp")
            quoted_per = _RATE_QUOTED_PER_HOURS.get(self.ex.id)
            if quoted_per:  # convert to the hourly rate actually settled each hour
                rate, interval = rate * interval / quoted_per, interval
                next_ts = next_ts or (now // 3_600_000 + 1) * 3_600_000
            mark, index = fr.get("markPrice"), fr.get("indexPrice")
            out[inst.key] = FundingSnapshot(
                instrument=inst,
                rate=rate,
                interval_hours=interval,
                ts=int(fr.get("timestamp") or now),
                next_funding_ts=next_ts,
                mark_price=float(mark) / inst.multiplier if mark else None,
                index_price=float(index) / inst.multiplier if index else None,
            )
        return out

    def fetch_quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        self.load_instruments()
        out: dict[str, Quote] = {}
        for kind in (Kind.SPOT, Kind.PERP):  # ccxt infers market type from the symbols
            group = {i.symbol: i for i in instruments if i.kind is kind}
            if not group:
                continue
            if self.ex.id == "kraken":  # symbols=... silently drops "inactive" pairs
                tickers = self.ex.fetch_tickers()
            else:
                tickers = self.ex.fetch_tickers(list(group))
            missing = []  # e.g. Hyperliquid spot tickers carry no bid/ask at all
            for sym, t in tickers.items():
                inst = group.get(sym)
                if inst is None:
                    continue
                if not t.get("bid") or not t.get("ask"):
                    missing.append((inst, t))
                    continue
                unit = inst.multiplier * (inst.contract_size if kind is Kind.PERP else 1.0)
                vol = t.get("quoteVolume")
                if vol is None and t.get("baseVolume") and t.get("last"):
                    vol = t["baseVolume"] * t["last"]
                out[inst.key] = Quote(
                    instrument=inst,
                    bid=t["bid"] / inst.multiplier,
                    ask=t["ask"] / inst.multiplier,
                    bid_size=(t.get("bidVolume") or 0) * unit or float("inf"),
                    ask_size=(t.get("askVolume") or 0) * unit or float("inf"),
                    ts=int(t.get("timestamp") or time.time() * 1000),
                    volume_usd_24h=float(vol) if vol else None,
                )
            out.update(self._book_quotes(missing))
        return out

    def _book_quotes(self, missing: list[tuple[Instrument, dict]]) -> dict[str, Quote]:
        """Order-book quotes for the most traded instruments whose ticker lacked bid/ask."""

        def volume(t: dict) -> float:
            if t.get("quoteVolume"):
                return float(t["quoteVolume"])
            return float(t.get("baseVolume") or 0) * float(t.get("last") or t.get("close") or 0)

        missing.sort(key=lambda it: -volume(it[1]))
        out: dict[str, Quote] = {}
        for inst, t in missing[:_BOOK_FALLBACK_MAX]:
            try:
                q = self.fetch_order_book_quote(inst)
            except Exception as exc:
                log.debug("%s: book quote for %s failed: %s", self.name, inst.symbol, exc)
                continue
            vol = volume(t)
            out[inst.key] = Quote(
                inst, q.bid, q.ask, q.bid_size, q.ask_size, q.ts, vol if vol else None
            )
        return out

    def fetch_touch(self, inst: Instrument) -> Quote:
        return self.fetch_order_book_quote(inst)

    def fetch_order_book_quote(self, inst: Instrument, depth: int = 5) -> Quote:
        """Top of book from the order book (sizes are more reliable than tickers)."""
        ob = self.ex.fetch_order_book(inst.symbol, depth)
        unit = inst.multiplier * (inst.contract_size if inst.is_perp else 1.0)
        (bp, bs), (ap, as_) = ob["bids"][0][:2], ob["asks"][0][:2]
        return Quote(
            inst,
            bp / inst.multiplier,
            ap / inst.multiplier,
            bs * unit,
            as_ * unit,
            int(ob.get("timestamp") or time.time() * 1000),
        )

    def fetch_funding_history(
        self, inst: Instrument, since_ms: int, until_ms: int | None = None
    ) -> list[tuple[int, float]]:
        """Settled funding ``[(ts_ms, rate_per_interval)]``, paging forward from ``since``.

        Not every venue honours ccxt's ``paginate`` (Hyperliquid returns one 500-row page),
        so we also page manually until a call adds nothing new."""
        if self.ex.id == "lighter":
            return self._lighter_funding_history(inst, since_ms, until_ms)
        end = until_ms or int(time.time() * 1000)
        merged: dict[int, float] = {}
        cursor = since_ms
        # ccxt's paginate for these venues steps a fixed 8h x page size, which skips months
        # on 1h/4h markets; page them ourselves with the venue's max page size instead
        page = _HISTORY_PAGE.get(self.ex.id)
        for _ in range(2_000):
            params: dict = {} if page else {"paginate": True, "paginationCalls": 200}
            if until_ms is not None:
                params["until"] = until_ms
            rows = self.ex.fetch_funding_rate_history(inst.symbol, cursor, page, params)
            fresh = {
                int(r["timestamp"]): float(r["fundingRate"])
                for r in rows
                if r.get("timestamp") is not None
                and r.get("fundingRate") is not None
                and int(r["timestamp"]) >= cursor
            }
            if not fresh or max(fresh) <= max(merged, default=-1):
                break
            merged.update(fresh)
            cursor = max(fresh) + 1
            if cursor >= end:
                break
        return sorted((ts, r) for ts, r in merged.items() if since_ms <= ts <= end)

    def _lighter_funding_history(
        self, inst: Instrument, since_ms: int, until_ms: int | None
    ) -> list[tuple[int, float]]:
        """Lighter /api/v1/fundings: hourly rows, ``rate`` an unsigned hourly *percent*,
        ``direction`` 'short' meaning shorts pay (negative funding), timestamps in seconds.
        Units come from third-party reverse engineering; check a few rows against the app."""
        market_id = int(self.ex.market(inst.symbol)["id"])
        end_s = (until_ms or int(time.time() * 1000)) // 1000
        start_s = since_ms // 1000
        out: dict[int, float] = {}
        while start_s < end_s:
            chunk_end = min(end_s, start_s + 700 * 3600)  # API returns at most 750 rows
            resp = self.ex.publicGetFundings(
                {
                    "market_id": market_id,
                    "resolution": "1h",
                    "start_timestamp": start_s,
                    "end_timestamp": chunk_end,
                    "count_back": 0,
                }
            )
            for row in resp.get("fundings") or []:
                sign = -1.0 if str(row.get("direction", "")).lower() == "short" else 1.0
                ts_ms = int(row["timestamp"]) * 1000
                if ts_ms >= since_ms:
                    out[ts_ms] = sign * float(row["rate"]) / 100.0
            start_s = chunk_end
        return sorted(out.items())

    # ------------------------------------------------------------------ trading
    def _unit(self, inst: Instrument) -> float:
        """Canonical units per ccxt order ``amount``."""
        return inst.multiplier * (inst.contract_size if inst.is_perp else 1.0)

    def round_qty(self, inst: Instrument, qty: float) -> float:
        """Round *down* to the venue's lot size (ccxt truncates by default)."""
        unit = self._unit(inst)
        try:
            return float(self.ex.amount_to_precision(inst.symbol, qty / unit)) * unit
        except Exception:  # ccxt raises when the amount truncates to zero
            return 0.0

    def min_qty(self, inst: Instrument) -> float:
        market = self.ex.market(inst.symbol)
        mn = ((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0
        return float(mn) * self._unit(inst)

    def min_notional(self, inst: Instrument) -> float:
        market = self.ex.market(inst.symbol)
        return float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0.0)

    @property
    def supports_post_only(self) -> bool:  # type: ignore[override]
        return bool(self.ex.has.get("createPostOnlyOrder") or self.ex.has.get("createOrder"))

    def _order_state(self, inst: Instrument, order: dict) -> OrderState:
        unit = self._unit(inst)
        filled = float(order.get("filled") or 0.0) * unit
        avg = order.get("average") or (order.get("price") if filled else None)
        fee_usd = 0.0
        base = self.ex.market(inst.symbol).get("base")
        for fee in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            cost = float(fee.get("cost") or 0.0)
            if fee.get("currency") == base:
                filled -= cost * inst.multiplier
                fee_usd += cost * float(avg or 0.0)
            else:
                fee_usd += cost
        return OrderState(
            str(order.get("id")),
            filled,
            float(avg) / inst.multiplier if avg else None,
            fee_usd,
            order.get("status") == "open",
        )

    def place_post_only(self, inst, order_side, qty, price, reduce_only=False) -> str | None:
        import ccxt

        self._require_trading()
        unit = self._unit(inst)
        amount = float(self.ex.amount_to_precision(inst.symbol, qty / unit))
        px = float(self.ex.price_to_precision(inst.symbol, price * inst.multiplier))
        params: dict = {**_ORDER_PARAMS.get(self.ex.id, {}), "postOnly": True}
        if reduce_only and inst.is_perp:
            params["reduceOnly"] = True
        cid = self._client_id(params)
        try:
            order = self.ex.create_order(inst.symbol, "limit", order_side, amount, px, params)
        except ccxt.NetworkError:
            # the exchange may have taken the order although the reply was lost: find it
            # by our id and carry on with it (or confirm it isn't there) before giving up
            found = self._find_by_client_id(inst, cid) if cid else None
            if found is None:
                raise
            order = found
        except ccxt.OrderImmediatelyFillable:
            return None
        except ccxt.InvalidOrder as exc:  # venues word "would cross" rejections differently
            if "post" in str(exc).lower():
                return None
            raise
        except ccxt.ExchangeError as exc:  # e.g. Coinbase INVALID_LIMIT_PRICE_POST_ONLY
            if "POST_ONLY" in str(exc):
                return None
            raise
        ended = order.get("status") in ("canceled", "cancelled", "rejected", "expired")
        if ended and not float(order.get("filled") or 0.0):
            return None  # some venues accept then cancel a post-only that would cross
        return str(order["id"])

    def fetch_order_state(self, inst, order_id) -> OrderState:
        order = self._retry(lambda: self.ex.fetch_order(order_id, inst.symbol), "fetch_order")
        return self._order_state(inst, order)

    def cancel_order(self, inst, order_id) -> OrderState:
        import ccxt

        def cancel() -> None:
            # an order that already filled or is gone can make the cancel itself fail;
            # the order's own state below is what counts (a repeated cancel is harmless)
            quiet = (ccxt.OrderNotFound, ccxt.BadRequest, ccxt.InvalidOrder, ccxt.CancelPending)
            with contextlib.suppress(*quiet):
                self.ex.cancel_order(order_id, inst.symbol)

        self._retry(cancel, "cancel_order")
        return self._settled(inst, order_id)

    def _retry(self, fn, what: str, tries: int = 5):
        """Retry a call that is safe to repeat (a read, a cancel) through short network
        trouble: timeouts, 5xx, Kraken's EService:Unavailable/Busy (0.5, 1, 2, 4 s)."""
        import ccxt

        for i in range(tries):
            try:
                return fn()
            except ccxt.NetworkError as exc:
                if i == tries - 1:
                    raise
                log.warning("%s: %s failed (%s); retrying", self.name, what, exc)
                time.sleep(0.5 * 2**i)
        return None

    def _client_id(self, params: dict) -> str | None:
        """Tag an order with our own id where the venue lets us look it up by it, so an
        order whose create reply was lost can still be found (Kraken: cl_ord_id)."""
        if self.ex.id != "kraken":
            return None
        cid = str(uuid.uuid4())
        params["clientOrderId"] = cid
        return cid

    def _find_by_client_id(self, inst: Instrument, cid: str) -> dict | None:
        """The order we tagged with ``cid``, open or closed, or None once it is clear
        that the exchange never took it. Raises if that can't be established."""
        import ccxt

        p = {"clientOrderId": cid}
        for i in range(5):
            if i:
                time.sleep(0.5 * 2 ** (i - 1))
            try:
                hits = self.ex.fetch_open_orders(inst.symbol, None, None, dict(p))
                hits = hits or self.ex.fetch_closed_orders(inst.symbol, None, None, dict(p))
            except ccxt.NetworkError as exc:
                log.warning("%s: looking up order %s failed: %s", self.name, cid, exc)
                continue
            if hits:
                log.warning("%s: order %s was placed although its reply was lost", self.name, cid)
                return hits[0]
            if i >= 2:  # asked three times with no network error: it isn't there
                return None
        raise RuntimeError(f"{self.name}: could not tell whether order {cid} was placed")

    def _settled(self, inst, order_id, tries: int = 6, pause_s: float = 0.5) -> OrderState:
        """Read an order until the venue reports it closed (cancels can be asynchronous).
        Still open after that means unknown: the executor then halts entries."""
        st = self.fetch_order_state(inst, order_id)
        for _ in range(tries - 1):
            if not st.is_open:
                break
            time.sleep(pause_s)
            st = self.fetch_order_state(inst, order_id)
        return st

    def _confirm_ioc(self, inst: Instrument, oid: str, tries: int = 6) -> dict:
        """The final state of an IOC whose create reply didn't say what filled (Kraken's
        AddOrder returns only an id). Never assume "nothing filled": that would send the
        same order again and leave the first fill unhedged and unrecorded. Reads until the
        order is final, cancels it if it still looks open, and raises if that can't be
        confirmed (the executor then treats the trade as unsafe and halts entries)."""
        import ccxt

        def read() -> dict | None:
            last = None
            for i in range(tries):
                if i:
                    time.sleep(0.5)
                try:
                    last = self.ex.fetch_order(oid, inst.symbol)
                except Exception as exc:
                    log.warning("%s: fetch_order %s failed: %s", self.name, oid, exc)
                    continue
                if last.get("status") not in (None, "open"):
                    return last
            return last

        order = read()
        if order is not None and order.get("status") not in (None, "open"):
            return order
        with contextlib.suppress(ccxt.OrderNotFound, ccxt.BadRequest, ccxt.InvalidOrder):
            self.ex.cancel_order(oid, inst.symbol)  # an IOC still "open" is pending: end it
        order = read()
        if order is None or order.get("status") in (None, "open"):
            raise RuntimeError(f"{self.name}: IOC {oid} fill could not be confirmed")
        return order

    def _require_trading(self) -> None:
        if not self.can_trade:
            raise RuntimeError(f"{self.name}: ccxt cannot confirm fills here; trading disabled")

    def place_ioc(
        self,
        inst: Instrument,
        order_side: str,
        qty: float,
        limit_price: float,
        reduce_only: bool = False,
    ) -> Fill:
        self._require_trading()
        unit = self._unit(inst)
        amount = float(self.ex.amount_to_precision(inst.symbol, qty / unit))
        price = float(self.ex.price_to_precision(inst.symbol, limit_price * inst.multiplier))
        params: dict = {**_ORDER_PARAMS.get(self.ex.id, {}), "timeInForce": "IOC"}
        if reduce_only and inst.is_perp:
            params["reduceOnly"] = True
        import ccxt

        cid = self._client_id(params)
        try:
            order = self.ex.create_order(inst.symbol, "limit", order_side, amount, price, params)
        except ccxt.NetworkError:
            found = self._find_by_client_id(inst, cid) if cid else None
            if found is None:
                raise
            order = found
        except ccxt.InvalidOrder as exc:
            # Hyperliquid reports an IOC with nothing to match as an error, not a 0 fill
            if "immediately match" in str(exc).lower():
                return Fill(inst, order_side, 0.0, limit_price, 0.0, None)
            raise
        if order.get("filled") is None or order.get("status") in (None, "open"):
            order = self._confirm_ioc(inst, str(order["id"]))
        filled = float(order.get("filled") or 0.0)
        avg = float(order.get("average") or order.get("price") or price)
        qty_filled = filled * unit
        fee_usd = 0.0
        market = self.ex.market(inst.symbol)
        for fee in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            cost = float(fee.get("cost") or 0.0)
            if fee.get("currency") == market.get("base"):
                # spot fee charged in the coin itself shrinks what we actually hold
                qty_filled -= cost * inst.multiplier
                fee_usd += cost * avg
            else:
                fee_usd += cost  # quote/settle currency, treated as USD
        return Fill(inst, order_side, qty_filled, avg / inst.multiplier, fee_usd, order.get("id"))

    def fetch_perp_positions(self) -> dict[str, PerpPositionInfo]:
        self.load_instruments()
        out: dict[str, PerpPositionInfo] = {}
        if Kind.PERP not in self.kinds:
            return out
        for p in self.ex.fetch_positions():
            inst = self._by_symbol.get(p.get("symbol"))
            contracts = float(p.get("contracts") or 0.0)
            if inst is None or contracts == 0:
                continue
            sign = -1.0 if p.get("side") == "short" else 1.0
            m = inst.multiplier

            def norm(v, m=m):
                return float(v) / m if v else None

            out[inst.key] = PerpPositionInfo(
                inst,
                sign * contracts * self._unit(inst),
                entry_price=norm(p.get("entryPrice")),
                mark_price=norm(p.get("markPrice")),
                liquidation_price=norm(p.get("liquidationPrice")),
            )
        return out

    def fetch_spot_balance(self, inst: Instrument) -> float:
        base = self.ex.market(inst.symbol)["base"]
        totals = self.ex.fetch_balance().get("total") or {}
        # Kraken auto-earn keeps tradable coins under a suffixed code such as "BTC.F"
        held = sum(float(v or 0) for k, v in totals.items() if k == base or k == f"{base}.F")
        return held * inst.multiplier

    def heartbeat(self) -> None:
        """Refresh the venue's cancel-all-after timer, if configured, so resting maker orders
        are pulled when the bot dies (note: it cancels *all* open orders on the account)."""
        if self.dead_man_seconds and self.ex.has.get("cancelAllOrdersAfter"):
            try:
                self.ex.cancel_all_orders_after(self.dead_man_seconds * 1000)
            except Exception as exc:
                log.warning("%s: dead-man switch refresh failed: %s", self.name, exc)

    def fetch_funding_payments(self, inst: Instrument, since_ms: int) -> list[tuple[int, float]]:
        rows = self.ex.fetch_funding_history(inst.symbol, since_ms)
        return [(int(r["timestamp"]), float(r["amount"])) for r in rows if r.get("amount")]
