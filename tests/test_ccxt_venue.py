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
