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
            with open(os.path.expanduser(os.environ[f"{name}_FILE"])) as f:
                value = f.read().strip()
        if value:
            out[field] = value.replace("\\n", "\n")
    return out


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
    ):
        self.name = name
        self.quotes = tuple(quotes)  # accepted quote/settle currencies, most preferred first
        self.kinds = {Kind(k) for k in kinds}
        self.aliases = aliases
        if exchange is None:
            import ccxt

            klass = getattr(ccxt, ccxt_id or name)
            exchange = klass(
                {"enableRateLimit": True, **(credentials or {}), "options": options or {}}
            )
        self.ex = exchange
        self._by_symbol: dict[str, Instrument] = {}
        self._intervals: dict[str, float] = {}

    # ------------------------------------------------------------------ market data
    def load_instruments(self) -> list[Instrument]:
        if self._by_symbol:
            return list(self._by_symbol.values())
        markets = self.ex.load_markets()
        best_spot: dict[str, Instrument] = {}  # one spot market per asset: best-ranked quote
        for sym, m in markets.items():
            if m.get("active") is False or m.get("expiry"):
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
            asset, mult = canonical_asset(m["base"], self.aliases)
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
                or DEFAULT_INTERVAL_HOURS.get(self.name, 8.0)
            )
            mark, index = fr.get("markPrice"), fr.get("indexPrice")
            out[inst.key] = FundingSnapshot(
                instrument=inst,
                rate=float(rate),
                interval_hours=interval,
                ts=int(fr.get("timestamp") or now),
                next_funding_ts=fr.get("fundingTimestamp") or fr.get("nextFundingTimestamp"),
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
            tickers = self.ex.fetch_tickers(list(group))
            for sym, t in tickers.items():
                inst = group.get(sym)
                if inst is None or not t.get("bid") or not t.get("ask"):
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
        params: dict = {"paginate": True, "paginationCalls": 500}
        if until_ms is not None:
            params["until"] = until_ms
        rows = self.ex.fetch_funding_rate_history(inst.symbol, since_ms, None, params)
        merged = {
            int(r["timestamp"]): float(r["fundingRate"])
            for r in rows
            if r.get("timestamp") is not None and r.get("fundingRate") is not None
        }
        return sorted((ts, r) for ts, r in merged.items() if ts >= since_ms)

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

        unit = self._unit(inst)
        amount = float(self.ex.amount_to_precision(inst.symbol, qty / unit))
        px = float(self.ex.price_to_precision(inst.symbol, price * inst.multiplier))
        params: dict = {"postOnly": True}
        if reduce_only and inst.is_perp:
            params["reduceOnly"] = True
        try:
            order = self.ex.create_order(inst.symbol, "limit", order_side, amount, px, params)
        except ccxt.OrderImmediatelyFillable:
            return None
        except ccxt.InvalidOrder as exc:  # venues word "would cross" rejections differently
            if "post" in str(exc).lower():
                return None
            raise
        if order.get("status") in ("canceled", "cancelled", "rejected", "expired"):
            return None  # some venues accept then cancel a post-only that would cross
        return str(order["id"])

    def fetch_order_state(self, inst, order_id) -> OrderState:
        return self._order_state(inst, self.ex.fetch_order(order_id, inst.symbol))

    def cancel_order(self, inst, order_id) -> OrderState:
        import ccxt

        with contextlib.suppress(ccxt.OrderNotFound):  # already filled/gone: state says which
            self.ex.cancel_order(order_id, inst.symbol)
        st = self.fetch_order_state(inst, order_id)
        st.is_open = False
        return st

    def place_ioc(
        self,
        inst: Instrument,
        order_side: str,
        qty: float,
        limit_price: float,
        reduce_only: bool = False,
    ) -> Fill:
        unit = self._unit(inst)
        amount = float(self.ex.amount_to_precision(inst.symbol, qty / unit))
        price = float(self.ex.price_to_precision(inst.symbol, limit_price * inst.multiplier))
        params: dict = {"timeInForce": "IOC"}
        if reduce_only and inst.is_perp:
            params["reduceOnly"] = True
        order = self.ex.create_order(inst.symbol, "limit", order_side, amount, price, params)
        if order.get("filled") is None or order.get("status") in (None, "open"):
            try:
                order = self.ex.fetch_order(order["id"], inst.symbol)
            except Exception as exc:  # fall back to what create_order returned
                log.warning("%s: fetch_order %s failed: %s", self.name, order.get("id"), exc)
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
        total = (self.ex.fetch_balance().get("total") or {}).get(base) or 0.0
        return float(total) * inst.multiplier

    def fetch_funding_payments(self, inst: Instrument, since_ms: int) -> list[tuple[int, float]]:
        rows = self.ex.fetch_funding_history(inst.symbol, since_ms)
        return [(int(r["timestamp"]), float(r["amount"])) for r in rows if r.get("amount")]
