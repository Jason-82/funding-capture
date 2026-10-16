"""Coinbase Advanced: spot through ccxt, US perpetual-style futures (CDE) through raw v3 calls.

ccxt 4.5.x mis-handles Coinbase's US perpetual-style futures: they load as dated futures,
funding is never read, CFM positions are mis-parsed, and bulk tickers ask for spot. This
adapter keeps ccxt for authentication and spot, and talks to these endpoints directly:

* ``GET market/products?product_type=FUTURE``: perps are the rows whose
  ``future_product_details.funding_interval`` is set ('3600s'). ``funding_rate`` there is
  an hourly decimal and ``funding_time`` the next hourly settlement.
* ``GET market/product_book``: top of book (sizes in contracts).
* ``POST orders`` / ``GET orders/historical/{id}`` / ``POST orders/batch_cancel``.
* ``GET cfm/positions`` and ``GET cfm/balance_summary``.

Contracts: orders are whole contracts. ``contract_size`` is in the contract's display
unit: "1000SHIB PERP" quotes price per 1,000 SHIB and one contract is 10,000 of those.

Not verified against a live account (written offline from Coinbase's SDK and API dumps).
Start with one contract and check every field ``fcap check`` prints.
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from datetime import datetime

from ..models import Fill, FundingSnapshot, Instrument, Kind, OrderState, PerpPositionInfo, Quote
from ..symbols import canonical_asset
from .ccxt_venue import CcxtVenue

log = logging.getLogger(__name__)

_OPEN_STATES = {"OPEN", "PENDING", "QUEUED", "CANCEL_QUEUED"}


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_in(text: str):
    i = text.find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except ValueError:
        return None


def _ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


class CoinbaseVenue(CcxtVenue):
    reports_funding = False  # no funding-payment history in the Advanced Trade API

    def __init__(
        self,
        name: str,
        ccxt_id: str | None = None,
        quotes: tuple[str, ...] = ("USD", "USDC"),
        *args,
        ioc_config: str = "sor_limit_ioc",
        **kwargs,
    ):
        super().__init__(name, ccxt_id, quotes, *args, **kwargs)
        self.ioc_config = ioc_config
        self._products: dict[str, dict] = {}  # product_id -> last products row
        self._products_ms = 0

    # ------------------------------------------------------------------ helpers
    def _perp_kind_enabled(self) -> bool:
        return Kind.PERP in self.kinds

    def _refresh_products(self, max_age_ms: int = 5_000) -> dict[str, dict]:
        now = int(time.time() * 1000)
        if now - self._products_ms > max_age_ms:
            resp = self.ex.v3PublicGetBrokerageMarketProducts({"product_type": "FUTURE"})
            self._products = {p["product_id"]: p for p in resp.get("products") or []}
            self._products_ms = now
        return self._products

    @staticmethod
    def _is_perp(p: dict) -> bool:
        fpd = p.get("future_product_details") or {}
        if not fpd.get("funding_interval"):
            return False  # dated future
        if str(fpd.get("non_crypto")).lower() == "true":
            return False  # equity-index perps etc.
        return not any(
            str(p.get(k)).lower() == "true"
            for k in ("is_disabled", "trading_disabled", "view_only", "cancel_only")
        )

    @staticmethod
    def _base_unit(p: dict) -> str:
        fpd = p.get("future_product_details") or {}
        name = fpd.get("contract_display_name") or p.get("display_name") or ""
        token = name.split()[0] if name.split() else ""
        return token or fpd.get("contract_root_unit") or p.get("base_currency_id") or ""

    def _unit(self, inst: Instrument) -> float:
        if inst.is_perp:  # canonical units per contract
            return inst.multiplier * inst.contract_size
        return super()._unit(inst)

    def _price_step(self, inst: Instrument) -> float:
        p = self._products.get(inst.symbol) or {}
        return _f(p.get("price_increment"), 0.0)

    def _venue_price(self, inst: Instrument, price: float, side: str) -> str:
        """Canonical price -> venue price string on the tick grid (rounded passively)."""
        px = price * inst.multiplier
        step = self._price_step(inst)
        if step > 0:
            ticks = px / step  # tolerance: a price already on the grid must stay put
            n = math.floor(ticks + 1e-9) if side == "buy" else math.ceil(ticks - 1e-9)
            px = n * step
        return f"{px:.10f}".rstrip("0").rstrip(".")

    # ------------------------------------------------------------------ market data
    def load_instruments(self) -> list[Instrument]:
        if self._by_symbol:
            return list(self._by_symbol.values())
        if Kind.SPOT in self.kinds:
            super().load_instruments()  # spot via ccxt; ccxt's CDE markets are skipped there
            # ccxt also lists Coinbase International's perps (BTC/USDC:USDC, AAPL/USDC:USDC)
            # as swaps. US accounts can't trade them; US perps come from the list below.
            self._by_symbol = {s: i for s, i in self._by_symbol.items() if not i.is_perp}
        if self._perp_kind_enabled():
            for pid, p in self._refresh_products(0).items():
                if not self._is_perp(p):
                    continue
                fpd = p.get("future_product_details") or {}
                asset, mult = canonical_asset(self._base_unit(p), self.aliases)
                if fpd.get("contract_root_unit"):  # e.g. 'ZCASH PERP' has root unit ZEC
                    asset = canonical_asset(fpd["contract_root_unit"], self.aliases)[0]
                size = _f(fpd.get("contract_size"), 0.0)
                if not asset or size <= 0:
                    continue
                self._by_symbol[pid] = Instrument(
                    self.name, pid, asset, Kind.PERP, "USD", mult, size
                )
        return list(self._by_symbol.values())

    def fetch_funding(self, instruments=None) -> dict[str, FundingSnapshot]:
        perps = self._perps(instruments)
        if not perps:
            return {}
        products = self._refresh_products()
        now = int(time.time() * 1000)
        out: dict[str, FundingSnapshot] = {}
        for inst in perps:
            p = products.get(inst.symbol)
            if p is None:
                continue
            fpd = p.get("future_product_details") or {}
            mark = _f(p.get("mid_market_price")) or _f(p.get("price"))
            index = _f(fpd.get("index_price"))
            next_ts = _ms(fpd.get("funding_time"))
            if next_ts is not None and next_ts <= now:  # API dumps show the last settlement
                next_ts += ((now - next_ts) // 3_600_000 + 1) * 3_600_000
            out[inst.key] = FundingSnapshot(
                instrument=inst,
                rate=_f(fpd.get("funding_rate")),
                interval_hours=1.0,
                ts=now,
                next_funding_ts=next_ts,
                mark_price=mark / inst.multiplier if mark else None,
                index_price=index / inst.multiplier if index else None,
            )
        return out

    def fetch_quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        spots = [i for i in instruments if not i.is_perp]
        out = super().fetch_quotes(spots) if spots else {}
        perps = [i for i in instruments if i.is_perp]
        if not perps:
            return out
        products = self._refresh_products()
        for inst in perps:
            p = products.get(inst.symbol)
            if p is None:
                continue
            bid, ask = _f(p.get("best_bid_price")), _f(p.get("best_ask_price"))
            if not (bid and ask):
                try:
                    q = self.fetch_touch(inst)
                except Exception as exc:
                    log.debug("coinbase book for %s failed: %s", inst.symbol, exc)
                    continue
                bid, ask = q.bid * inst.multiplier, q.ask * inst.multiplier
                sizes = (q.bid_size, q.ask_size)
            else:
                sizes = (math.inf, math.inf)
            out[inst.key] = Quote(
                inst,
                bid / inst.multiplier,
                ask / inst.multiplier,
                sizes[0],
                sizes[1],
                int(time.time() * 1000),
                _f(p.get("approximate_quote_24h_volume")) or None,
            )
        return out

    def fetch_touch(self, inst: Instrument) -> Quote:
        if not inst.is_perp:
            return super().fetch_touch(inst)
        book = (
            self.ex.v3PublicGetBrokerageMarketProductBook(
                {"product_id": inst.symbol, "limit": 1}
            ).get("pricebook")
            or {}
        )
        (bid,), (ask,) = book.get("bids")[:1], book.get("asks")[:1]
        unit = self._unit(inst)
        return Quote(
            inst,
            _f(bid["price"]) / inst.multiplier,
            _f(ask["price"]) / inst.multiplier,
            _f(bid["size"]) * unit,
            _f(ask["size"]) * unit,
            int(time.time() * 1000),
        )

    def fetch_funding_history(self, inst, since_ms, until_ms=None):
        if inst.is_perp:
            log.info(
                "coinbase has no funding-history API; build history with `fcap record` "
                "(or download the CSV from coinbase.com/derivatives)"
            )
            return []
        return super().fetch_funding_history(inst, since_ms, until_ms)

    # ------------------------------------------------------------------ trading
    def round_qty(self, inst: Instrument, qty: float) -> float:
        if not inst.is_perp:
            return super().round_qty(inst, qty)
        unit = self._unit(inst)
        return math.floor(qty / unit + 1e-9) * unit

    def min_qty(self, inst: Instrument) -> float:
        return self._unit(inst) if inst.is_perp else super().min_qty(inst)

    def min_notional(self, inst: Instrument) -> float:
        return 0.0 if inst.is_perp else super().min_notional(inst)

    def _create(self, inst, side, contracts, config_name, limit_px, post_only=False) -> dict:
        """POST an order; a rejection comes back as Coinbase's response dict.

        ccxt raises ExchangeError('coinbase ' + body) for any reply carrying
        ``error_response`` (even HTTP 200); we recover the body so callers can read the
        rejection reason."""
        import ccxt

        cfg = {"base_size": str(int(contracts)), "limit_price": limit_px}
        if config_name == "limit_limit_gtc":
            cfg["post_only"] = post_only
        try:
            return self.ex.v3PrivatePostBrokerageOrders(
                {
                    "client_order_id": f"fc-{uuid.uuid4()}",
                    "product_id": inst.symbol,
                    "side": side.upper(),
                    "order_configuration": {config_name: cfg},
                }
            )
        except ccxt.ExchangeError as exc:
            body = _json_in(str(exc))
            if isinstance(body, dict) and ("error_response" in body or "success" in body):
                return body
            raise

    @staticmethod
    def _order_id(resp: dict) -> tuple[str | None, str]:
        ok = (resp.get("success_response") or {}).get("order_id") or resp.get("order_id")
        err = resp.get("error_response") or {}
        reason = str(err.get("error") or err.get("preview_failure_reason") or "")
        reason += " " + str(resp.get("failure_reason") or "")
        return (ok if resp.get("success", bool(ok)) else None), reason.strip()

    def _state(self, inst: Instrument, order_id: str) -> OrderState:
        o = (
            self.ex.v3PrivateGetBrokerageOrdersHistoricalOrderId({"order_id": order_id}).get(
                "order"
            )
            or {}
        )
        filled = _f(o.get("filled_size")) * self._unit(inst)
        avg = _f(o.get("average_filled_price"))
        return OrderState(
            order_id,
            filled,
            avg / inst.multiplier if avg else None,
            _f(o.get("total_fees")),
            str(o.get("status", "")).upper() in _OPEN_STATES,
        )

    def place_ioc(self, inst, order_side, qty, limit_price, reduce_only=False) -> Fill:
        if not inst.is_perp:
            return super().place_ioc(inst, order_side, qty, limit_price, reduce_only)
        contracts = round(self.round_qty(inst, qty) / self._unit(inst))
        empty = Fill(inst, order_side, 0.0, limit_price, 0.0, None)
        if contracts < 1:
            return empty
        # an IOC rounds onto the tick grid aggressively (at most one tick past our limit);
        # _venue_price rounds passively for the side it is given, so pass the opposite side
        px = self._venue_price(inst, limit_price, "sell" if order_side == "buy" else "buy")
        resp = self._create(inst, order_side, contracts, self.ioc_config, px)
        oid, reason = self._order_id(resp)
        if oid is None and "UNSUPPORTED_ORDER_CONFIGURATION" in reason.upper():
            log.warning("coinbase rejected %s for futures; retrying as FOK", self.ioc_config)
            self.ioc_config = "limit_limit_fok"
            resp = self._create(inst, order_side, contracts, self.ioc_config, px)
            oid, reason = self._order_id(resp)
        if oid is None:
            raise RuntimeError(f"coinbase order rejected: {reason or resp}")
        st = self._state(inst, oid)
        for _ in range(10):  # IOC/FOK resolve at once; allow a moment for the record
            if not st.is_open:
                break
            time.sleep(0.3)
            st = self._state(inst, oid)
        if st.is_open:
            st = self.cancel_order(inst, oid)
        return Fill(inst, order_side, st.filled, st.avg_price or limit_price, st.fee_usd, oid)

    def place_post_only(self, inst, order_side, qty, price, reduce_only=False):
        if not inst.is_perp:
            import ccxt

            try:
                return super().place_post_only(inst, order_side, qty, price, reduce_only)
            except ccxt.ExchangeError as exc:
                if "POST_ONLY" in str(exc):  # INVALID_LIMIT_PRICE_POST_ONLY: would cross
                    return None
                raise
        contracts = round(self.round_qty(inst, qty) / self._unit(inst))
        if contracts < 1:
            return None
        px = self._venue_price(inst, price, order_side)
        oid, reason = self._order_id(
            self._create(inst, order_side, contracts, "limit_limit_gtc", px, post_only=True)
        )
        if oid is None:
            if "POST_ONLY" in reason.upper():
                return None  # would have crossed
            raise RuntimeError(f"coinbase post-only rejected: {reason}")
        return oid

    def fetch_order_state(self, inst, order_id) -> OrderState:
        if not inst.is_perp:
            return super().fetch_order_state(inst, order_id)
        return self._state(inst, order_id)

    def cancel_order(self, inst, order_id) -> OrderState:
        """Cancel via batch_cancel (its per-order result is ignored: an order that just
        filled 'fails' to cancel) and return the confirmed final state, fills included."""
        self.ex.v3PrivatePostBrokerageOrdersBatchCancel({"order_ids": [order_id]})
        return self._settled(inst, order_id)

    def fetch_perp_positions(self) -> dict[str, PerpPositionInfo]:
        if not self._perp_kind_enabled():
            return {}
        self.load_instruments()
        out: dict[str, PerpPositionInfo] = {}
        for row in self.ex.v3PrivateGetBrokerageCfmPositions().get("positions") or []:
            inst = self._by_symbol.get(row.get("product_id"))
            n = _f(row.get("number_of_contracts"))
            if inst is None or n == 0:
                continue
            sign = -1.0 if str(row.get("side", "")).upper() == "SHORT" else 1.0
            entry, mark = _f(row.get("avg_entry_price")), _f(row.get("current_price"))
            out[inst.key] = PerpPositionInfo(
                inst,
                sign * abs(n) * self._unit(inst),
                entry / inst.multiplier if entry else None,
                mark / inst.multiplier if mark else None,
                None,  # Coinbase reports margin per account, not a liquidation price
            )
        return out

    def margin_buffer(self) -> float | None:
        """Account-level cushion before liquidation (fraction), if Coinbase reports it."""
        try:
            s = self.ex.v3PrivateGetBrokerageCfmBalanceSummary().get("balance_summary") or {}
        except Exception as exc:
            log.debug("coinbase balance_summary failed: %s", exc)
            return None
        pct = s.get("liquidation_buffer_percentage")
        return _f(pct) / 100 if pct not in (None, "") else None
