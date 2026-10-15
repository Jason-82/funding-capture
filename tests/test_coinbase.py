"""CoinbaseVenue (US perpetual-style futures) against a fake v3 API with Coinbase's shapes."""

import pytest

from funding_capture.models import Kind
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
            "contract_root_unit": display.split()[0].removeprefix("1000"),
            "funding_interval": interval,
            "funding_rate": rate if interval else "",
            "funding_time": "2026-10-15T15:00:00Z",
            "index_price": str(price),
            "non_crypto": extra.get("non_crypto", False),
        },
    }


class FakeCoinbaseAPI:
    id = "coinbase"
    has = {}

    def __init__(self):
        self.products = [
            _product("BIP-20DEC30-CDE", "BTC PERP", 0.01, 71525, 5),
            _product("SHP-20DEC30-CDE", "1000SHIB PERP", 10000, 0.00553, 0.00001, "0.000038"),
            _product("BIT-26JUN26-CDE", "BTC 26 JUN 26", 0.01, 71800, 5, interval=None),
            _product("US5-19DEC30-CDE", "US500 PERP", 1, 6000, 0.25, non_crypto=True),
        ]
        self.orders: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.reject_ioc = False
        self.positions = []

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
            return {
                "success": False,
                "error_response": {"error": "UNSUPPORTED_ORDER_CONFIGURATION"},
            }
        if (
            kind == "limit_limit_gtc"
            and cfg.get("post_only")
            and body["side"] == "BUY"
            and float(cfg["limit_price"]) >= 0.00554
        ):
            return {"success": False, "error_response": {"error": "INVALID_LIMIT_PRICE_POST_ONLY"}}
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
        for oid in body["order_ids"]:
            self.orders[oid]["status"] = "CANCELLED"
        return {"results": [{"success": True, "order_id": o} for o in body["order_ids"]]}

    def v3PrivateGetBrokerageCfmPositions(self):
        return {"positions": self.positions}

    def v3PrivateGetBrokerageCfmBalanceSummary(self):
        return {"balance_summary": {"liquidation_buffer_percentage": "42.5"}}


@pytest.fixture
def cb():
    v = CoinbaseVenue("coinbase", exchange=FakeCoinbaseAPI(), kinds=("perp",))
    insts = {i.symbol: i for i in v.load_instruments()}
    return v, insts


def test_discovers_perps_only_with_units(cb):
    v, insts = cb
    assert set(insts) == {"BIP-20DEC30-CDE", "SHP-20DEC30-CDE"}  # no dated / index futures
    btc, shib = insts["BIP-20DEC30-CDE"], insts["SHP-20DEC30-CDE"]
    assert (btc.asset, btc.multiplier, btc.contract_size, btc.kind) == ("BTC", 1, 0.01, Kind.PERP)
    # price is per 1,000 SHIB and a contract is 10,000 of those = 10M SHIB
    assert (shib.asset, shib.multiplier, shib.contract_size) == ("SHIB", 1000, 10000)
    assert v.min_qty(shib) == 10_000_000
    assert v.round_qty(shib, 25_000_000) == 20_000_000  # whole contracts only


def test_funding_is_hourly_with_next_settlement(cb):
    v, insts = cb
    snaps = v.fetch_funding()
    s = snaps[insts["SHP-20DEC30-CDE"].key]
    assert s.interval_hours == 1 and s.rate == pytest.approx(0.000038)
    assert s.mark_price == pytest.approx(0.00553 / 1000)
    assert s.next_funding_ts == 1_792_076_400_000  # 2026-10-15T15:00Z
    assert v.reports_funding is False


def test_touch_in_canonical_units(cb):
    v, insts = cb
    q = v.fetch_touch(insts["SHP-20DEC30-CDE"])
    assert q.bid == pytest.approx(0.00552 / 1000) and q.ask == pytest.approx(0.00554 / 1000)
    assert q.bid_size == 40 * 10_000_000 and q.ask_size == 25 * 10_000_000


def test_ioc_order_contracts_ticks_and_fok_fallback(cb):
    v, insts = cb
    shib = insts["SHP-20DEC30-CDE"]
    v._refresh_products(0)
    f = v.place_ioc(shib, "sell", 30_000_000, limit_price=0.0055149 / 1000)
    body = v.ex.sent[-1]
    assert body["side"] == "SELL" and body["product_id"] == "SHP-20DEC30-CDE"
    cfg = body["order_configuration"]["sor_limit_ioc"]
    assert cfg["base_size"] == "3"
    assert cfg["limit_price"] == "0.00551"  # sells round down: at most one tick more aggressive
    assert f.qty == 30_000_000 and f.fee_usd == pytest.approx(0.40)
    assert f.price == pytest.approx(0.00551 / 1000)

    v.ex.reject_ioc = True
    f = v.place_ioc(shib, "buy", 10_000_000, limit_price=0.0055 / 1000)
    assert "limit_limit_fok" in v.ex.sent[-1]["order_configuration"] and f.qty == 10_000_000
    assert v.ioc_config == "limit_limit_fok"  # remembered


def test_post_only_rounds_passively_and_detects_cross(cb):
    v, insts = cb
    shib = insts["SHP-20DEC30-CDE"]
    v._refresh_products(0)
    oid = v.place_post_only(shib, "buy", 10_000_000, 0.0055299 / 1000)
    cfg = v.ex.sent[-1]["order_configuration"]["limit_limit_gtc"]
    assert cfg == {"base_size": "1", "limit_price": "0.00552", "post_only": True}
    assert v.fetch_order_state(shib, oid).is_open
    st = v.cancel_order(shib, oid)
    assert not st.is_open and st.filled == 0
    assert v.place_post_only(shib, "buy", 10_000_000, 0.00554 / 1000) is None  # would cross


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
