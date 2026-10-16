"""CcxtVenue unit conversions, run offline against real ccxt exchange objects.

Markets are injected with ``set_markets`` and network methods are stubbed, so ccxt's own
precision handling runs for real while nothing touches the network.
"""

import ccxt
import pytest

from funding_capture.models import Kind
from funding_capture.venues.ccxt_venue import CcxtVenue, credentials_from_env


def _market(symbol, base, kind, contract_size=1.0, amount_step=0.001, price_step=0.01):
    swap = kind == "swap"
    quote = "USDT"
    return {
        "id": symbol.replace("/", "").replace(":USDT", ""),
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "settle": quote if swap else None,
        "baseId": base,
        "quoteId": quote,
        "settleId": quote if swap else None,
        "type": kind,
        "spot": not swap,
        "swap": swap,
        "future": False,
        "option": False,
        "contract": swap,
        "linear": True if swap else None,
        "inverse": False if swap else None,
        "contractSize": contract_size if swap else None,
        "active": True,
        "precision": {"amount": amount_step, "price": price_step},
        "limits": {"amount": {"min": amount_step, "max": None}, "price": {}, "cost": {}},
        "info": {},
    }


@pytest.fixture
def okx():
    ex = ccxt.okx()
    ex.set_markets(
        [
            _market("BTC/USDT:USDT", "BTC", "swap", contract_size=0.01, amount_step=0.1),
            _market("BTC/USDT", "BTC", "spot", amount_step=0.00001),
            _market(
                "PEPE/USDT:USDT",
                "PEPE",
                "swap",
                contract_size=1e7,
                amount_step=0.1,
                price_step=1e-10,
            ),
            _market("BTC/USD:BTC", "BTC", "swap"),  # inverse-settled: ignored
        ]
    )
    ex.markets["BTC/USD:BTC"]["linear"] = False
    ex.markets["BTC/USD:BTC"]["settle"] = "BTC"
    return CcxtVenue("okx", exchange=ex)


@pytest.fixture
def binance():
    ex = ccxt.binance()
    ex.set_markets(
        [_market("1000PEPE/USDT:USDT", "1000PEPE", "swap", amount_step=1, price_step=1e-7)]
    )
    return CcxtVenue("binance", exchange=ex)


def test_instruments(okx, binance):
    insts = {i.symbol: i for i in okx.load_instruments()}
    assert set(insts) == {"BTC/USDT:USDT", "BTC/USDT", "PEPE/USDT:USDT"}
    assert insts["BTC/USDT:USDT"].contract_size == 0.01
    assert insts["BTC/USDT"].kind is Kind.SPOT
    (pepe,) = binance.load_instruments()
    assert (pepe.asset, pepe.multiplier) == ("PEPE", 1000)


def test_funding_normalisation(binance, okx):
    (pepe,) = binance.load_instruments()
    binance.ex.fetch_funding_rates = lambda symbols: {
        pepe.symbol: {
            "symbol": pepe.symbol,
            "fundingRate": 0.0003,
            "markPrice": 0.012,  # price of 1000 PEPE
            "fundingTimestamp": 123,
            "interval": "4h",
            "timestamp": 1,
        }
    }
    snap = binance.fetch_funding()[pepe.key]
    assert snap.interval_hours == 4
    assert snap.hourly == pytest.approx(0.0003 / 4)
    assert snap.mark_price == pytest.approx(0.000012)  # per single PEPE
    assert snap.next_funding_ts == 123

    okx.load_instruments()
    okx.ex.fetch_funding_rates = lambda symbols: {
        "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001}
    }
    (snap,) = okx.fetch_funding().values()
    assert snap.interval_hours == 8  # venue default when ccxt gives no interval


def test_quotes_convert_contract_sizes(okx):
    okx.load_instruments()
    okx.ex.fetch_tickers = lambda symbols: {
        s: {"bid": 100.0, "ask": 101.0, "bidVolume": 50, "askVolume": 20, "quoteVolume": 1e6}
        for s in symbols
    }
    perp = next(i for i in okx.load_instruments() if i.symbol == "BTC/USDT:USDT")
    q = okx.fetch_quotes([perp])[perp.key]
    assert q.bid_size == pytest.approx(0.5)  # 50 contracts x 0.01 BTC
    assert q.ask_size == pytest.approx(0.2)
    assert q.volume_usd_24h == 1e6


def test_round_qty_uses_contract_lots(okx):
    perp = next(i for i in okx.load_instruments() if i.symbol == "BTC/USDT:USDT")
    assert okx.round_qty(perp, 0.0123) == pytest.approx(0.012)  # 1.23 -> 1.2 contracts
    assert okx.round_qty(perp, 0.0004) == 0.0  # below one lot


def test_place_ioc_translates_units_and_fees(okx, binance):
    perp = next(i for i in okx.load_instruments() if i.symbol == "BTC/USDT:USDT")
    sent = {}

    def create_order(symbol, type_, side, amount, price, params):
        sent.update(symbol=symbol, type=type_, side=side, amount=amount, price=price, **params)
        return {
            "id": "1",
            "status": "closed",
            "filled": amount,
            "average": 100.0,
            "fee": {"cost": 0.05, "currency": "USDT"},
        }

    okx.ex.create_order = create_order
    f = okx.place_ioc(perp, "sell", 0.02, 99.5, reduce_only=True)
    assert sent["amount"] == pytest.approx(2.0)  # contracts
    assert sent["timeInForce"] == "IOC" and sent["reduceOnly"] is True
    assert f.qty == pytest.approx(0.02) and f.price == 100.0 and f.fee_usd == 0.05

    spot = next(i for i in okx.load_instruments() if i.kind is Kind.SPOT)
    okx.ex.create_order = lambda *a, **k: {
        "id": "2",
        "status": "closed",
        "filled": 0.01,
        "average": 100.0,
        "fee": {"cost": 0.00001, "currency": "BTC"},  # fee taken in the coin bought
    }
    f = okx.place_ioc(spot, "buy", 0.01, 101)
    assert f.qty == pytest.approx(0.00999)
    assert f.fee_usd == pytest.approx(0.001)

    (pepe,) = binance.load_instruments()
    binance.ex.create_order = lambda symbol, t, side, amount, price, params: (
        sent.update(amount=amount, price=price)
        or {"id": "3", "status": "closed", "filled": amount, "average": price}
    )
    f = binance.place_ioc(pepe, "buy", 5_000_000, 0.000012)
    assert sent["amount"] == 5000  # 1000PEPE units
    assert sent["price"] == pytest.approx(0.012)
    assert f.qty == pytest.approx(5_000_000) and f.price == pytest.approx(0.000012)


def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "k")
    monkeypatch.setenv("OKX_SECRET", "s")
    monkeypatch.setenv("OKX_PASSWORD", "p")
    assert credentials_from_env("okx") == {"apiKey": "k", "secret": "s", "password": "p"}
    assert credentials_from_env("bybit") == {}


# --------------------------------------------------------------------- venue quirks
def _hl():
    ex = ccxt.hyperliquid({"options": {"builderFee": False}})
    kpepe = _market("KPEPE/USDC:USDC", "KPEPE", "swap", amount_step=1, price_step=1e-6)
    kpepe.update(
        quote="USDC",
        settle="USDC",
        baseName="kPEPE",
        limits={"amount": {"min": None}, "price": {}, "cost": {"min": 10}},
    )
    btc = _market("BTC/USDC:USDC", "BTC", "swap", amount_step=0.00001, price_step=1)
    btc.update(
        quote="USDC",
        settle="USDC",
        baseName="BTC",
        limits={"amount": {"min": None}, "price": {}, "cost": {"min": 10}},
    )
    ubtc = _market("BTC/USDC", "BTC", "spot", amount_step=0.00001, price_step=1)
    ubtc.update(quote="USDC", baseName="UBTC")
    ex.set_markets([kpepe, btc, ubtc])
    return CcxtVenue("hyperliquid", exchange=ex, quotes=("USDC",))


def test_default_options_disable_builder_fees():
    v = CcxtVenue("hyperliquid")
    assert v.ex.options["builderFee"] is False
    assert v.ex.options["fetchMarkets"]["types"] == ["spot", "swap"]  # no HIP-3 stock perps
    v2 = CcxtVenue("hyperliquid", options={"builderFee": True})  # explicit config wins
    assert v2.ex.options["builderFee"] is True


def test_hyperliquid_k_prefixed_perps_and_min_notional():
    v = _hl()
    insts = {i.symbol: i for i in v.load_instruments()}
    assert (insts["KPEPE/USDC:USDC"].asset, insts["KPEPE/USDC:USDC"].multiplier) == ("PEPE", 1e3)
    assert insts["BTC/USDC"].kind is Kind.SPOT and insts["BTC/USDC"].asset == "BTC"
    assert v.min_notional(insts["BTC/USDC:USDC"]) == 10


def test_ioc_that_cannot_match_is_a_zero_fill():
    v = _hl()
    perp = next(i for i in v.load_instruments() if i.symbol == "BTC/USDC:USDC")

    def reject(*a, **k):
        raise ccxt.InvalidOrder("Order could not immediately match against any resting orders.")

    v.ex.create_order = reject
    assert v.place_ioc(perp, "buy", 0.001, 60_000).qty == 0

    def other(*a, **k):
        raise ccxt.InvalidOrder("Order must have minimum value of $10")

    v.ex.create_order = other
    with pytest.raises(ccxt.InvalidOrder):
        v.place_ioc(perp, "buy", 0.00001, 60_000)


def test_spot_without_ticker_bid_ask_uses_order_book():
    v = _hl()
    spot_btc = next(i for i in v.load_instruments() if i.symbol == "BTC/USDC")
    v.ex.fetch_tickers = lambda symbols: {
        s: {"bid": None, "ask": None, "close": 60_000, "quoteVolume": 5e6} for s in symbols
    }
    v.ex.fetch_order_book = lambda symbol, limit=None: {
        "bids": [[59_990, 0.5]],
        "asks": [[60_010, 0.4]],
        "timestamp": 1,
    }
    q = v.fetch_quotes([spot_btc])[spot_btc.key]
    assert (q.bid, q.ask, q.ask_size, q.volume_usd_24h) == (59_990, 60_010, 0.4, 5e6)


def test_history_pages_forward_when_venue_returns_one_page():
    v = _hl()
    perp = next(i for i in v.load_instruments() if i.symbol == "BTC/USDC:USDC")
    h = 3_600_000
    calls = []

    def page(symbol, since, limit, params):  # 500-row pages, like Hyperliquid
        calls.append(since)
        first = -(-since // h) * h  # settlements sit on the hour grid
        return [
            {"timestamp": first + k * h, "fundingRate": 1e-5}
            for k in range(500)
            if first + k * h <= 1_200 * h
        ]

    v.ex.fetch_funding_rate_history = page
    rows = v.fetch_funding_history(perp, 0, until_ms=1_200 * h)
    assert len(rows) == 1_201 and rows[0][0] == 0 and rows[-1][0] == 1_200 * h
    assert len(calls) == 3


def test_lighter_rates_are_normalised_to_hourly():
    ex = ccxt.lighter()
    m = _market("ETH/USDC:USDC", "ETH", "swap")
    m.update(id="0", quote="USDC", settle="USDC")
    ex.set_markets([m])
    v = CcxtVenue("lighter", exchange=ex, quotes=("USDC",))
    (eth,) = v.load_instruments()
    # /funding-rates reports an 8h-equivalent fraction (ccxt passes it through as-is)
    ex.fetch_funding_rates = lambda symbols: {eth.symbol: {"fundingRate": 0.000096}}
    snap = v.fetch_funding()[eth.key]
    assert snap.interval_hours == 1
    assert snap.rate == pytest.approx(0.000012)  # = 0.0012% per hour, as the app shows
    assert snap.next_funding_ts % 3_600_000 == 0

    calls = []

    def fundings(params):
        calls.append(params)
        s = params["start_timestamp"]
        return {
            "fundings": [
                {"timestamp": s + 3600, "rate": "0.0012", "direction": "long"},
                {"timestamp": s + 7200, "rate": "0.0030", "direction": "short"},
            ]
        }

    ex.publicGetFundings = fundings
    rows = v.fetch_funding_history(eth, 0, until_ms=3_600_000 * 10)
    assert calls[0]["market_id"] == 0 and calls[0]["resolution"] == "1h"
    assert rows == [(3_600_000, pytest.approx(0.000012)), (7_200_000, pytest.approx(-0.00003))]


def _kraken():
    ex = ccxt.kraken()
    a = _market("BTC/USD", "BTC", "spot", amount_step=0.0001)
    a.update(quote="USD", info={"status": "online"})
    b = _market("WIF/USD", "WIF", "spot", amount_step=0.01)  # no status -> ccxt: inactive
    b.update(quote="USD", active=False, info={})
    c = _market("OLD/USD", "OLD", "spot")
    c.update(quote="USD", active=False, info={"status": "delisted"})
    ex.set_markets([a, b, c])
    return CcxtVenue("kraken", exchange=ex, quotes=("USD", "USDC"), kinds=("spot",))


def test_kraken_keeps_pairs_without_status_and_fetches_all_tickers():
    v = _kraken()
    assert {i.asset for i in v.load_instruments()} == {"BTC", "WIF"}
    calls = []

    def tickers(symbols=None):
        calls.append(symbols)
        return {"WIF/USD": {"bid": 1.0, "ask": 1.01, "quoteVolume": 3e6}}

    v.ex.fetch_tickers = tickers
    wif = next(i for i in v.load_instruments() if i.asset == "WIF")
    assert v.fetch_quotes([wif])[wif.key].ask == 1.01
    assert calls == [None]  # asking by symbol would silently drop WIF


def test_kraken_orders_take_fee_in_usd_and_balance_counts_earn():
    v = _kraken()
    btc = next(i for i in v.load_instruments() if i.asset == "BTC")
    sent = {}

    def create_order(symbol, type_, side, amount, price, params):
        sent.update(params)
        return {"id": "k1", "status": "closed", "filled": amount, "average": price}

    v.ex.create_order = create_order
    v.place_ioc(btc, "sell", 0.01, 60_000)
    assert sent["oflags"] == "fciq" and sent["timeInForce"] == "IOC"
    sent.clear()
    v.place_post_only(btc, "buy", 0.01, 59_000)
    assert sent["oflags"] == "fciq" and sent["postOnly"] is True
    v.ex.fetch_balance = lambda: {"total": {"BTC": 0.5, "BTC.F": 0.25, "USD": 10}}
    assert v.fetch_spot_balance(btc) == pytest.approx(0.75)


def test_per_contract_minimum_fee():
    from funding_capture.costs import CostModel, VenueFees
    from funding_capture.models import Instrument

    shib = Instrument("coinbase", "SHP-20DEC30-CDE", "SHIB", Kind.PERP, "USD", 1000, 10000)
    costs = CostModel(
        fees={"coinbase": VenueFees(perp_taker=0.0002, perp_min_fee_per_contract=0.20)},
        slippage_bps=0,
    )
    # one contract = 10M SHIB; at $0.00000553 that is $55.30, so $0.20 is 36 bps
    assert costs.leg_cost(shib, price=0.00553 / 1000) == pytest.approx(0.20 / 55.3)
    assert costs.leg_cost(shib) == pytest.approx(0.0002)  # no price: percentage only


def test_kraken_ioc_fill_is_confirmed_never_assumed_zero(monkeypatch):
    import funding_capture.venues.ccxt_venue as cv

    monkeypatch.setattr(cv.time, "sleep", lambda s: None)
    v = _kraken()
    btc = next(i for i in v.load_instruments() if i.asset == "BTC")
    v.ex.create_order = lambda *a, **k: {"id": "O1", "status": None, "filled": None}
    reads = []

    def flaky(oid, symbol):  # one timeout, then the real answer
        reads.append(oid)
        if len(reads) == 1:
            raise ccxt.RequestTimeout("kraken timed out")
        return {"id": oid, "status": "closed", "filled": 0.07, "average": 60_000}

    v.ex.fetch_order = flaky
    assert v.place_ioc(btc, "buy", 0.1, 60_000).qty == pytest.approx(0.07)

    states = iter(["open", "open", "open", "open", "open", "open", "canceled"])
    cancels = []
    v.ex.fetch_order = lambda oid, symbol: {
        "id": oid,
        "status": next(states),
        "filled": 0.05,
        "average": 60_000,
    }
    v.ex.cancel_order = lambda oid, symbol: cancels.append(oid)
    assert v.place_ioc(btc, "buy", 0.1, 60_000).qty == pytest.approx(0.05)  # pending: ended
    assert cancels == ["O1"]

    def down(oid, symbol):
        raise ccxt.ExchangeNotAvailable("EService:Unavailable")

    v.ex.fetch_order = down
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        v.place_ioc(btc, "buy", 0.1, 60_000)


def test_kraken_reads_and_cancels_ride_out_short_outages(monkeypatch):
    import funding_capture.venues.ccxt_venue as cv

    monkeypatch.setattr(cv.time, "sleep", lambda s: None)
    v = _kraken()
    btc = next(i for i in v.load_instruments() if i.asset == "BTC")
    blips = {"read": 1, "cancel": 1}
    cancels = []

    def read(oid, symbol):
        if blips["read"]:
            blips["read"] -= 1
            raise ccxt.RequestTimeout("timed out")
        return {"id": oid, "status": "canceled", "filled": 0.02, "average": 60_000}

    def cancel(oid, symbol):
        cancels.append(oid)
        if blips["cancel"]:
            blips["cancel"] -= 1
            raise ccxt.ExchangeNotAvailable("EService:Unavailable")

    v.ex.fetch_order = read
    v.ex.cancel_order = cancel
    st = v.cancel_order(btc, "O7")
    assert cancels == ["O7", "O7"] and st.filled == pytest.approx(0.02) and not st.is_open


def test_kraken_order_whose_reply_was_lost_is_found_by_our_id(monkeypatch):
    import funding_capture.venues.ccxt_venue as cv

    monkeypatch.setattr(cv.time, "sleep", lambda s: None)
    v = _kraken()
    btc = next(i for i in v.load_instruments() if i.asset == "BTC")
    placed = {}

    def create(symbol, type_, side, amount, price, params):
        placed["cid"] = params["clientOrderId"]
        raise ccxt.RequestTimeout("reply lost; Kraken took the order")

    v.ex.create_order = create
    v.ex.fetch_open_orders = lambda symbol, since, limit, params: (
        [{"id": "O9", "status": "open", "filled": 0.0}]
        if params.get("clientOrderId") == placed["cid"]
        else []
    )
    v.ex.fetch_closed_orders = lambda *a: []
    assert v.place_post_only(btc, "buy", 0.01, 59_000) == "O9"  # carried on with, not lost


def test_kraken_lost_reply_never_assumes_the_order_is_gone(monkeypatch):
    """Not finding the order right after a lost reply doesn't prove Kraken refused it: it
    can still show up and rest (post-only) or execute (IOC). So the post-only is cancelled
    by our id and the outcome is unknown; only Kraken's own refusal codes skip that."""
    import funding_capture.venues.ccxt_venue as cv
    from funding_capture.venues.base import OrderOutcomeUnknown

    monkeypatch.setattr(cv.time, "sleep", lambda s: None)
    v = _kraken()
    btc = next(i for i in v.load_instruments() if i.asset == "BTC")
    sent, cancels, reads = {}, [], {"n": 0}

    def create(symbol, type_, side, amount, price, params):
        sent["cid"] = params["clientOrderId"]
        raise sent["exc"]

    def open_orders(symbol, since, limit, params):
        reads["n"] += 1
        return []

    v.ex.create_order = create
    v.ex.fetch_open_orders = open_orders
    v.ex.fetch_closed_orders = lambda *a: []
    v.ex.cancel_order = lambda oid, symbol, params: cancels.append(params["clientOrderId"])

    sent["exc"] = ccxt.RequestTimeout("reply lost")
    with pytest.raises(OrderOutcomeUnknown, match="Open orders"):
        v.place_post_only(btc, "buy", 0.01, 59_000)
    assert cancels == [sent["cid"]]  # pulled by our id in case it shows up later
    with pytest.raises(OrderOutcomeUnknown):
        v.place_ioc(btc, "buy", 0.01, 61_000)

    # lookups that fail don't count as "not there": six failed reads are still unknown
    def flaky(symbol, since, limit, params):
        reads["n"] += 1
        raise ccxt.RequestTimeout("lookup lost too")

    v.ex.fetch_open_orders, reads["n"], cancels[:] = flaky, 0, []
    with pytest.raises(OrderOutcomeUnknown):
        v.place_post_only(btc, "buy", 0.01, 59_000)
    assert reads["n"] == 6 and cancels == [sent["cid"]]
    # two failed reads then three clean misses: five reads, not cut short at three
    left = {"fail": 2}

    def mixed(symbol, since, limit, params):
        reads["n"] += 1
        if left["fail"]:
            left["fail"] -= 1
            raise ccxt.RequestTimeout("lookup lost")
        return []

    v.ex.fetch_open_orders, reads["n"] = mixed, 0
    assert v._find_by_client_id(btc, "x") is None and reads["n"] == 5

    # Kraken's own refusal (busy, rate limit) means the order was never taken: no lookup
    v.ex.fetch_open_orders, reads["n"], cancels[:] = open_orders, 0, []
    for refused in (
        ccxt.ExchangeNotAvailable('kraken {"error":["EService:Busy"]}'),
        ccxt.DDoSProtection('kraken {"error":["EAPI:Rate limit exceeded"]}'),
    ):
        sent["exc"] = refused
        with pytest.raises(type(refused)):
            v.place_post_only(btc, "buy", 0.01, 59_000)
    assert reads["n"] == 0 and not cancels
