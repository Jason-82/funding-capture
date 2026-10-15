"""CoinbaseVenue (US perpetual-style futures) against a fake v3 API with Coinbase's shapes.

Rejections are raised the way real ccxt raises them: ExchangeError('coinbase ' + body)
for any reply carrying ``error_response``.
"""

import json
import time
from pathlib import Path

import ccxt
import pytest

from funding_capture.models import Instrument, Kind
from funding_capture.venues.coinbase import CoinbaseVenue


def _product(pid, display, size, price, tick, rate="0.000020", interval="3600s", **extra):
    return {
        "product_id": pid,
        "price": str(price),
        "mid_market_price": str(price),
        "price_increment": str(tick),
        "approximate_quote_24h_volume": "2500000",
        "display_name": display,
        "is_disabled": False,
        "trading_disabled": False,
        "view_only": False,
        "future_product_details": {
            "contract_display_name": display,
            "contract_size": str(size),
            "contract_root_unit": extra.get("root", display.split()[0].removeprefix("1000")),
            "funding_interval": interval,
            "funding_rate": rate if interval else "",
            "funding_time": "2099-01-01T00:00:00Z",
            "index_price": str(price),
            "non_crypto": extra.get("non_crypto", False),
        },
    }


class FakeCoinbaseAPI:
    id = "coinbase"
    has = {"fetchOrder": True}

    def __init__(self):
        self.products = [
            _product("BIP-20DEC30-CDE", "BTC PERP", 0.01, 71525, 5),
            _product("SHP-20DEC30-CDE", "1000SHIB PERP", 10000, 0.00553, 0.00001, "0.000038"),
            _product("BIT-26JUN26-CDE", "BTC 26 JUN 26", 0.01, 71800, 5, interval=None),
            _product("US5-19DEC30-CDE", "US500 PERP", 1, 6000, 0.25, non_crypto=True),
            _product("ZEC-20DEC30-CDE", "ZCASH PERP", 1, 40, 0.01, root="ZEC"),
        ]
        self.orders: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.reject_ioc = False
        self.positions = []

    @staticmethod
    def _reject(error):
        body = {"success": False, "error_response": {"error": error}}
        raise ccxt.ExchangeError("coinbase " + json.dumps(body))

    def v3PublicGetBrokerageMarketProducts(self, params):
        assert params == {"product_type": "FUTURE"}
        return {"products": self.products}

    def v3PublicGetBrokerageMarketProductBook(self, params):
        assert params["product_id"] == "SHP-20DEC30-CDE"
        return {
            "pricebook": {
                "bids": [{"price": "0.00552", "size": "40"}],
                "asks": [{"price": "0.00554", "size": "25"}],
            }
        }

    def v3PrivatePostBrokerageOrders(self, body):
        self.sent.append(body)
        ((kind, cfg),) = body["order_configuration"].items()
        if kind == "sor_limit_ioc" and self.reject_ioc:
            self._reject("UNSUPPORTED_ORDER_CONFIGURATION")
        crosses = body["side"] == "BUY" and float(cfg["limit_price"]) >= 0.00554
        if kind == "limit_limit_gtc" and cfg.get("post_only") and crosses:
            self._reject("INVALID_LIMIT_PRICE_POST_ONLY")
        oid = f"o{len(self.orders) + 1}"
        filled = cfg["base_size"] if kind != "limit_limit_gtc" else "0"
        self.orders[oid] = {
            "order_id": oid,
            "status": "FILLED" if filled != "0" else "OPEN",
            "filled_size": filled,
            "average_filled_price": cfg["limit_price"],
            "total_fees": "0.40",
        }
        return {"success": True, "success_response": {"order_id": oid}}

    def v3PrivateGetBrokerageOrdersHistoricalOrderId(self, params):
        return {"order": self.orders[params["order_id"]]}

    def v3PrivatePostBrokerageOrdersBatchCancel(self, body):
        results = []
        for oid in body["order_ids"]:
            o = self.orders.get(oid)
            if o is not None and o["status"] == "OPEN":
                o["status"] = "CANCELLED"
                results.append({"success": True, "order_id": oid})
            else:  # e.g. already filled: Coinbase reports a failed cancel
                results.append({"success": False, "failure_reason": "UNKNOWN_CANCEL_ORDER"})
        return {"results": results}

    def v3PrivateGetBrokerageCfmPositions(self):
        return {"positions": self.positions}

    def v3PrivateGetBrokerageCfmBalanceSummary(self):
        return {"balance_summary": {"liquidation_buffer_percentage": "42.5"}}


@pytest.fixture
def cb():
    v = CoinbaseVenue("coinbase", exchange=FakeCoinbaseAPI(), kinds=("perp",))
    insts = {i.symbol: i for i in v.load_instruments()}
    v._refresh_products(0)
    return v, insts


def test_discovers_perps_only_with_units(cb):
    v, insts = cb
    # dated futures and the equity-index perp are excluded
    assert set(insts) == {"BIP-20DEC30-CDE", "SHP-20DEC30-CDE", "ZEC-20DEC30-CDE"}
    btc, shib = insts["BIP-20DEC30-CDE"], insts["SHP-20DEC30-CDE"]
    assert (btc.asset, btc.multiplier, btc.contract_size, btc.kind) == ("BTC", 1, 0.01, Kind.PERP)
    # price is per 1,000 SHIB and a contract is 10,000 of those = 10M SHIB
    assert (shib.asset, shib.multiplier, shib.contract_size) == ("SHIB", 1000, 10000)
    assert insts["ZEC-20DEC30-CDE"].asset == "ZEC"  # displayed as 'ZCASH PERP'
    assert v.min_qty(shib) == 10_000_000
    assert v.round_qty(shib, 25_000_000) == 20_000_000  # whole contracts only


def test_funding_is_hourly_with_next_settlement(cb):
    v, insts = cb
    s = v.fetch_funding()[insts["SHP-20DEC30-CDE"].key]
    assert s.interval_hours == 1 and s.rate == pytest.approx(0.000038)
    assert s.mark_price == pytest.approx(0.00553 / 1000)
    assert s.next_funding_ts == 4_070_908_800_000  # 2099-01-01T00:00Z
    assert v.reports_funding is False


def test_past_funding_time_rolls_to_next_hour(cb):
    v, insts = cb
    for p in v.ex.products:  # what API dumps show: the settlement that just passed
        p["future_product_details"]["funding_time"] = "2020-01-01T00:00:00Z"
    v._refresh_products(0)
    nxt = v.fetch_funding()[insts["BIP-20DEC30-CDE"].key].next_funding_ts
    now = time.time() * 1000
    assert nxt % 3_600_000 == 0 and now < nxt <= now + 3_600_000


def test_touch_in_canonical_units(cb):
    v, insts = cb
    q = v.fetch_touch(insts["SHP-20DEC30-CDE"])
    assert q.bid == pytest.approx(0.00552 / 1000) and q.ask == pytest.approx(0.00554 / 1000)
    assert q.bid_size == 40 * 10_000_000 and q.ask_size == 25 * 10_000_000


def test_ioc_order_contracts_ticks_and_fok_fallback(cb):
    v, insts = cb
    shib = insts["SHP-20DEC30-CDE"]
    f = v.place_ioc(shib, "sell", 30_000_000, limit_price=0.0055149 / 1000)
    body = v.ex.sent[-1]
    assert body["side"] == "SELL" and body["product_id"] == "SHP-20DEC30-CDE"
    cfg = body["order_configuration"]["sor_limit_ioc"]
    assert cfg["base_size"] == "3"
    assert cfg["limit_price"] == "0.00551"  # sells round down: at most one tick more aggressive
    assert f.qty == 30_000_000 and f.fee_usd == pytest.approx(0.40)
    assert f.price == pytest.approx(0.00551 / 1000)

    v.ex.reject_ioc = True  # ccxt raises; the adapter must still see the reason and retry
    f = v.place_ioc(shib, "buy", 10_000_000, limit_price=0.0055 / 1000)
    assert "limit_limit_fok" in v.ex.sent[-1]["order_configuration"] and f.qty == 10_000_000
    assert v.ioc_config == "limit_limit_fok"  # remembered


def test_post_only_rounds_passively_and_detects_cross(cb):
    v, insts = cb
    shib = insts["SHP-20DEC30-CDE"]
    oid = v.place_post_only(shib, "buy", 10_000_000, 0.0055299 / 1000)
    cfg = v.ex.sent[-1]["order_configuration"]["limit_limit_gtc"]
    assert cfg == {"base_size": "1", "limit_price": "0.00552", "post_only": True}
    assert v.fetch_order_state(shib, oid).is_open
    st = v.cancel_order(shib, oid)
    assert not st.is_open and st.filled == 0
    assert v.place_post_only(shib, "buy", 10_000_000, 0.00554 / 1000) is None  # would cross


def test_cancel_of_a_filled_order_reports_the_fill(cb):
    v, insts = cb
    shib = insts["SHP-20DEC30-CDE"]
    oid = v.place_post_only(shib, "buy", 20_000_000, 0.00552 / 1000)
    v.ex.orders[oid].update(status="FILLED", filled_size="2")  # fills before we cancel
    st = v.cancel_order(shib, oid)
    assert st.filled == 20_000_000 and not st.is_open


def test_spot_cancel_ignores_failed_cancel_result_and_reads_state(cb):
    v, _ = cb
    spot = Instrument("coinbase", "SHIB/USD", "SHIB", Kind.SPOT, "USD")
    v.ex.market = lambda sym: {"base": "SHIB"}
    v.ex.fetch_order = lambda oid, sym: {
        "id": oid,
        "status": "closed",
        "filled": 1e6,
        "average": 0.0000055,
        "fee": None,
    }
    st = v.cancel_order(spot, "o99")  # batch_cancel says it failed: the order filled
    assert st.filled == 1e6 and not st.is_open


def test_positions_and_margin(cb):
    v, insts = cb
    v.ex.positions = [
        {
            "product_id": "BIP-20DEC30-CDE",
            "side": "SHORT",
            "number_of_contracts": "3",
            "avg_entry_price": "70000",
            "current_price": "71000",
        }
    ]
    (pos,) = v.fetch_perp_positions().values()
    assert pos.qty == pytest.approx(-0.03) and pos.entry_price == 70000
    assert pos.liquidation_price is None
    assert v.margin_buffer() == pytest.approx(0.425)


def test_cli_builds_coinbase_venue_from_us_config():
    from funding_capture.cli import _data_venues
    from funding_capture.config import load_settings

    cfg = Path(__file__).resolve().parent.parent / "config.us.yaml"
    venues = _data_venues(load_settings(cfg))
    assert isinstance(venues["coinbase"], CoinbaseVenue)
    assert venues["coinbase"].quotes == ("USD", "USDC")
    assert venues["kraken"].kinds == {Kind.SPOT}
