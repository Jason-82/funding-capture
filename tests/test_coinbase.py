"""CoinbaseVenue (US perpetual-style futures) against a fake v3 API with Coinbase's shapes.

Rejections are raised the way real ccxt raises them: ExchangeError('coinbase ' + body)
for any reply carrying ``error_response``.
"""

import json
import time
from pathlib import Path

import ccxt
import pytest
import requests
from conftest import spot

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


def test_coinbase_international_perps_are_left_out():
    api = FakeCoinbaseAPI()
    base = {"active": True, "base": "BTC"}
    api.load_markets = lambda: {
        "BTC/USD": {**base, "symbol": "BTC/USD", "spot": True, "quote": "USD"},
        "BTC/USDC:USDC": {
            **base,
            "symbol": "BTC/USDC:USDC",
            "swap": True,
            "linear": True,
            "quote": "USDC",
            "settle": "USDC",
            "contractSize": 1,
        },
    }
    v = CoinbaseVenue("coinbase", exchange=api, kinds=("spot", "perp"))
    kinds = {i.symbol: i.kind for i in v.load_instruments()}
    assert kinds["BTC/USD"] is Kind.SPOT
    assert kinds["BIP-20DEC30-CDE"] is Kind.PERP
    assert "BTC/USDC:USDC" not in kinds  # Coinbase International: not for US accounts


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


def test_reduce_only_never_buys_more_than_the_open_short(cb):
    v, insts = cb
    btc = insts["BIP-20DEC30-CDE"]
    v.ex.positions = [
        {
            "product_id": "BIP-20DEC30-CDE",
            "side": "SHORT",
            "number_of_contracts": "2",
            "avg_entry_price": "70000",
            "current_price": "71525",
        }
    ]
    f = v.place_ioc(btc, "buy", 0.05, 71600, reduce_only=True)  # 5 contracts asked
    assert v.ex.sent[-1]["order_configuration"]["sor_limit_ioc"]["base_size"] == "2"
    assert f.qty == pytest.approx(0.02)
    sent = len(v.ex.sent)
    assert v.place_ioc(btc, "sell", 0.05, 71500, reduce_only=True).qty == 0  # a short
    v.ex.positions = []
    assert v.place_ioc(btc, "buy", 0.05, 71600, reduce_only=True).qty == 0  # nothing open
    assert len(v.ex.sent) == sent  # neither was sent


def test_failed_perp_load_leaves_nothing_cached():
    api = FakeCoinbaseAPI()
    api.load_markets = lambda: {
        "BTC/USD": {
            "symbol": "BTC/USD",
            "spot": True,
            "quote": "USD",
            "base": "BTC",
            "active": True,
        }
    }
    real, calls = api.v3PublicGetBrokerageMarketProducts, []

    def flaky(params):
        calls.append(1)
        if len(calls) == 1:
            raise ccxt.RequestTimeout("market/products timed out")
        return real(params)

    api.v3PublicGetBrokerageMarketProducts = flaky
    v = CoinbaseVenue("coinbase", exchange=api, kinds=("spot", "perp"))
    with pytest.raises(ccxt.RequestTimeout):
        v.load_instruments()
    symbols = {i.symbol for i in v.load_instruments()}  # the retry loads everything
    assert {"BTC/USD", "BIP-20DEC30-CDE"} <= symbols


def test_halted_perp_is_not_traded_but_its_position_is_still_read():
    api = FakeCoinbaseAPI()
    api.products[0]["cancel_only"] = True  # BIP: Coinbase set it to cancel-only
    v = CoinbaseVenue("coinbase", exchange=api, kinds=("perp",))
    assert "BIP-20DEC30-CDE" not in {i.symbol for i in v.load_instruments()}
    api.positions = [
        {
            "product_id": "BIP-20DEC30-CDE",
            "side": "SHORT",
            "number_of_contracts": "2",
            "avg_entry_price": "70000",
            "current_price": "71525",
        }
    ]
    (pos,) = v.fetch_perp_positions().values()
    assert pos.instrument.symbol == "BIP-20DEC30-CDE" and pos.qty == pytest.approx(-0.02)
    with pytest.raises(RuntimeError, match="halted"):  # so nothing trades against it
        v.fetch_touch(pos.instrument)
    for flags in ({"post_only": True}, {"auction_mode": True}):  # reopening phases
        assert not CoinbaseVenue._tradeable(flags)
    assert not CoinbaseVenue._tradeable({"fcm_trading_session_details": {"is_session_open": False}})
    assert CoinbaseVenue._tradeable(api.products[1])
    api.positions[0]["side"] = "SOMETHING_NEW"
    with pytest.raises(ValueError, match="unexpected side"):
        v.fetch_perp_positions()  # unknown, never guessed as long


def test_ioc_fill_is_confirmed_and_a_lost_reply_is_resent_safely(cb, monkeypatch):
    import funding_capture.venues.coinbase as cbmod

    monkeypatch.setattr(cbmod.time, "sleep", lambda s: None)
    monkeypatch.setattr("funding_capture.venues.ccxt_venue.time.sleep", lambda s: None)
    v, insts = cb
    shib = insts["SHP-20DEC30-CDE"]
    api = v.ex
    real_post, posts = api.v3PrivatePostBrokerageOrders, []

    def lossy_post(body):
        posts.append(body["client_order_id"])
        if len(posts) == 1:
            raise ccxt.RequestTimeout("reply lost")
        return real_post(body)

    api.v3PrivatePostBrokerageOrders = lossy_post
    real_get, gets = api.v3PrivateGetBrokerageOrdersHistoricalOrderId, []

    def flaky_get(params):
        gets.append(1)
        if len(gets) == 1:
            raise ccxt.RequestTimeout("read timed out")
        return real_get(params)

    api.v3PrivateGetBrokerageOrdersHistoricalOrderId = flaky_get
    f = v.place_ioc(shib, "buy", 20_000_000, 0.0000056)
    assert posts[0] == posts[1]  # the same client_order_id: Coinbase won't place it twice
    assert f.qty == pytest.approx(20_000_000)  # confirmed despite a failed read
    api.v3PrivateGetBrokerageOrdersHistoricalOrderId = lambda params: {"order": {}}
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        v.place_ioc(shib, "buy", 10_000_000, 0.0000056)


def test_gateway_errors_are_retried_and_an_ambiguous_create_is_unknown(cb, monkeypatch):
    from funding_capture.venues.base import OrderOutcomeUnknown

    monkeypatch.setattr("funding_capture.venues.coinbase.time.sleep", lambda s: None)
    monkeypatch.setattr("funding_capture.venues.ccxt_venue.time.sleep", lambda s: None)
    v, insts = cb
    shib = insts["SHP-20DEC30-CDE"]
    api = v.ex
    real_post, real_get = (
        api.v3PrivatePostBrokerageOrders,
        (api.v3PrivateGetBrokerageOrdersHistoricalOrderId),
    )
    n = {"post": 1, "get": 1}

    def post(body):  # ccxt raises a gRPC-gateway body as plain ExchangeError
        if n["post"]:
            n["post"] -= 1
            raise ccxt.ExchangeError('coinbase {"error":"INTERNAL","code":13}')
        return real_post(body)

    def get(params):
        if n["get"]:
            n["get"] -= 1
            raise ccxt.ExchangeError('coinbase {"error":"UNAVAILABLE","code":14}')
        return real_get(params)

    api.v3PrivatePostBrokerageOrders, api.v3PrivateGetBrokerageOrdersHistoricalOrderId = post, get
    assert v.place_ioc(shib, "buy", 10_000_000, 0.0000056).qty == pytest.approx(10_000_000)

    def always(code):
        def fail(body):
            raise ccxt.ExchangeError(f'coinbase {{"error":"{code}","code":13}}')

        return fail

    real_cancel, n["cancel"] = api.v3PrivatePostBrokerageOrdersBatchCancel, 1

    def cancel(body):
        if n["cancel"]:
            n["cancel"] -= 1
            raise ccxt.ExchangeError('coinbase {"error":"UNAVAILABLE","code":14}')
        return real_cancel(body)

    api.v3PrivatePostBrokerageOrdersBatchCancel = cancel
    assert not v.cancel_order(shib, "o1").is_open  # retried, not raised

    api.v3PrivatePostBrokerageOrders = always("DEADLINE_EXCEEDED")  # may have gone through
    with pytest.raises(OrderOutcomeUnknown):
        v.place_ioc(shib, "buy", 10_000_000, 0.0000056)
    api.v3PrivatePostBrokerageOrders = always("UNAVAILABLE")  # the gateway may have sent it on
    with pytest.raises(OrderOutcomeUnknown):
        v.place_ioc(shib, "buy", 10_000_000, 0.0000056)
    posts = []

    def bare(status):  # how ccxt words a proxy's error page: '<id> <method> <url> <status> ...'
        def fail(body):
            posts.append(body["client_order_id"])
            raise ccxt.ExchangeNotAvailable(
                f"coinbase POST https://api.coinbase.com/api/v3/brokerage/orders {status} "
                f"Bad Gateway <html>{status}</html>"
            )

        return fail

    api.v3PrivatePostBrokerageOrders = bare(502)  # the order may be behind the proxy
    with pytest.raises(OrderOutcomeUnknown):
        v.place_ioc(shib, "buy", 10_000_000, 0.0000056)
    assert len(posts) == 6 and len(set(posts)) == 1  # resent under the same id each time
    api.v3PrivatePostBrokerageOrders = always("RESOURCE_EXHAUSTED")  # rate limit: never placed
    with pytest.raises(ccxt.ExchangeError):
        v.place_ioc(shib, "buy", 10_000_000, 0.0000056)
    api.v3PrivatePostBrokerageOrders = always("INVALID_ARGUMENT")
    with pytest.raises(ccxt.ExchangeError):
        v.place_ioc(shib, "buy", 10_000_000, 0.0000056)


def _replies(api, *errors, then=None):
    """Make order POSTs fail with these (real-ccxt) errors in turn, then go through."""
    real, sent, left = api.v3PrivatePostBrokerageOrders, [], list(errors)

    def post(body):
        sent.append(body["client_order_id"])
        if left:
            raise left.pop(0)
        return (then or real)(body)

    api.v3PrivatePostBrokerageOrders = post
    return sent


def test_order_create_outcomes_from_real_ccxt_errors(cb, monkeypatch):
    """Anything that doesn't say the order was refused may have placed it: it is resent
    under the same client_order_id (Coinbase won't place it twice) and ends as unknown,
    never as "not placed". Once a try may have placed it, a later refusal says nothing
    about that try."""
    from conftest import ccxt_reply_error as err

    from funding_capture.venues.base import OrderOutcomeUnknown

    monkeypatch.setattr("funding_capture.venues.coinbase.time.sleep", lambda s: None)
    v, insts = cb
    shib, api = insts["SHP-20DEC30-CDE"], v.ex
    real = api.v3PrivatePostBrokerageOrders
    timeout = err("coinbase", 0, requests.exceptions.ReadTimeout("Read timed out"))
    unknown_ = err("coinbase", 500, '{"error":"UNKNOWN","error_details":"context canceled"}')
    cancelled = err("coinbase", 499, '{"error":"CANCELLED"}')

    def buy():
        return v.place_ioc(shib, "buy", 10_000_000, 0.0000056)

    # gateway codes outside the old list, a v2 internal error, an unmapped status page
    for e in (
        unknown_,
        cancelled,
        err("coinbase", 500, '{"errors":[{"id":"internal_server_error","message":"x"}]}'),
        err("coinbase", 524, "<html>A timeout occurred</html>"),
        err("coinbase", 408, "Request Timeout"),
    ):
        api.v3PrivatePostBrokerageOrders = real
        sent = _replies(api, *[e] * 6)
        with pytest.raises(OrderOutcomeUnknown):
            buy()
        assert len(sent) == 6 and len(set(sent)) == 1
        api.v3PrivatePostBrokerageOrders = real
        sent = _replies(api, e)  # once, then the resend answers: carried on with
        assert buy().qty == pytest.approx(10_000_000) and len(set(sent)) == 1

    # an earlier try may have gone in: a later refusal or rejection is still unknown
    for later in (
        err("coinbase", 401, "Unauthorized"),
        err("coinbase", 401, '{"error":"UNAUTHENTICATED","code":16}'),
        err("coinbase", 400, '{"error":"INVALID_ARGUMENT","code":3}'),
        err("coinbase", 200, '{"success":false,"error_response":{"error":"INSUFFICIENT_FUND"}}'),
    ):
        api.v3PrivatePostBrokerageOrders = real
        sent = _replies(api, timeout, later)
        with pytest.raises(OrderOutcomeUnknown):
            buy()
        assert len(sent) == 2

    # refused on the first try: not placed, raised at once
    for first, cls in (
        (err("coinbase", 401, "Unauthorized"), ccxt.AuthenticationError),
        (err("coinbase", 400, '{"error":"INVALID_ARGUMENT","code":3}'), ccxt.BadRequest),
    ):
        api.v3PrivatePostBrokerageOrders = real
        sent = _replies(api, first)
        with pytest.raises(cls) as raised:
            buy()
        assert not isinstance(raised.value, OrderOutcomeUnknown) and len(sent) == 1
    # rate limits and a proxy's 4xx page never got through: resent, then raised as is
    for busy in (
        err("coinbase", 429, "Too Many Requests"),
        err("coinbase", 429, '{"error":"rate_limit_exceeded","error_description":"slow down"}'),
        err("coinbase", 403, "<html/>"),
    ):
        api.v3PrivatePostBrokerageOrders = real
        sent = _replies(api, *[busy] * 6)
        with pytest.raises(type(busy)):
            buy()
        assert len(sent) == 6
    # a rejection on the first try is Coinbase's answer: read by the caller
    api.v3PrivatePostBrokerageOrders = real
    _replies(api, err("coinbase", 200, '{"success":false,"error_response":{"error":"X"}}'))
    with pytest.raises(RuntimeError, match="rejected"):
        buy()
    # a post-only that may be resting says where to cancel it
    api.v3PrivatePostBrokerageOrders = real
    _replies(api, *[timeout] * 6)
    with pytest.raises(OrderOutcomeUnknown, match="Open orders"):
        v.place_post_only(shib, "buy", 10_000_000, 0.0000055)


def test_spot_orders_are_resent_under_our_own_id(cb, monkeypatch):
    """Spot goes through ccxt's create_order, which tags each call with a fresh random
    id; ours replaces it, so a create whose reply was lost is resent, not lost."""
    from conftest import ccxt_reply_error as err

    from funding_capture.venues.base import OrderOutcomeUnknown

    monkeypatch.setattr("funding_capture.venues.coinbase.time.sleep", lambda s: None)
    v, _ = cb
    btc = spot("coinbase", "BTC", "USD")
    timeout = err("coinbase", 0, requests.exceptions.ReadTimeout("Read timed out"))
    sent, left = [], []

    def create(symbol, type_, side, amount, price, params):
        sent.append(params["client_order_id"])
        if left:
            raise left.pop(0)
        return {"id": "s1", "status": "open", "filled": 0.0}

    v.ex.create_order = create
    left[:] = [timeout, timeout]
    assert v._create_order(btc, "buy", 0.01, 60_000, {}, post_only=True)["id"] == "s1"
    assert len(sent) == 3 and len(set(sent)) == 1 and sent[0].startswith("fc-")
    sent.clear()
    left[:] = [timeout] * 6
    with pytest.raises(OrderOutcomeUnknown, match="Open orders"):
        v._create_order(btc, "buy", 0.01, 60_000, {}, post_only=True)
    rejection = err("coinbase", 200, '{"success":false,"error_response":{"error":"X"}}')
    left[:] = [rejection]  # Coinbase's answer: raised as ccxt raised it, for the caller
    with pytest.raises(ccxt.ExchangeError) as raised:
        v._create_order(btc, "buy", 0.01, 60_000, {}, post_only=False)
    assert raised.value is rejection
