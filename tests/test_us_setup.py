"""End to end, offline: Kraken spot (worked as maker) + Coinbase US perp, paper-traded."""

import math

import pytest
from conftest import T0, FakeVenue, spot
from test_coinbase import FakeCoinbaseAPI, _product

from funding_capture.costs import CostModel, VenueFees
from funding_capture.engine import Engine, EngineConfig
from funding_capture.execution import ExecutionParams, PairExecutor
from funding_capture.forecast import ForecastParams
from funding_capture.models import Construction
from funding_capture.opportunities import StrategyParams
from funding_capture.risk import RiskLimits
from funding_capture.venues.coinbase import CoinbaseVenue
from funding_capture.venues.paper import PaperVenue


class BookAPI(FakeCoinbaseAPI):
    def __init__(self):
        super().__init__()
        self.products = [
            _product("DOP-20DEC30-CDE", "DOGE PERP", 5000, 0.1000, 0.00001, rate="0.0001")
        ]
        for p in self.products:
            p["future_product_details"]["funding_time"] = "2026-10-15T15:00:00Z"

    def v3PublicGetBrokerageMarketProductBook(self, params):
        return {
            "pricebook": {
                "bids": [{"price": "0.09995", "size": "50"}],
                "asks": [{"price": "0.10005", "size": "50"}],
            }
        }


def test_kraken_spot_plus_coinbase_perp_paper_round(tmp_path):
    cb_data = CoinbaseVenue("coinbase", exchange=BookAPI(), kinds=("perp",))
    doge = spot("kraken", "DOGE", quote="USD")
    kr_data = FakeVenue("kraken", [doge], lot=1)
    kr_data.set_quote(doge, 0.0999, 0.1001, size=1e7, volume=1e7)

    fees_cb = VenueFees(perp_taker=0.0002, perp_maker=0.0002, perp_min_fee_per_contract=0.20)
    fees_kr = VenueFees(spot_taker=0.008, spot_maker=0.004)
    venues = {"coinbase": PaperVenue(cb_data, fees_cb), "kraken": PaperVenue(kr_data, fees_kr)}

    polls = []

    def sleep(_):  # first poll: sellers take out our resting 0.0999 bid on Kraken
        polls.append(1)
        kr_data.set_quote(doge, 0.0997, 0.0999, size=1e7, volume=1e7)

    costs = CostModel(
        fees={"coinbase": fees_cb, "kraken": fees_kr},
        slippage_bps=5,
        maker_venues=("kraken", "coinbase"),
        perp_leverage=1.0,
    )
    strategy = StrategyParams(
        constructions=(Construction.SPOT_PERP,),
        entry_apr=0.10,
        horizon_hours=720,
        max_breakeven_hours=1000,
        forecast=ForecastParams(
            halflife_hours=48, reversion_halflife_hours=math.inf, anchor_hourly=0.0
        ),
    )
    executor = PairExecutor(
        venues,
        ExecutionParams(max_slice_usd=2000),
        costs.maker_venues,
        sleep=sleep,
        clock=lambda: float(len(polls)),
    )
    eng = Engine(
        venues,
        strategy,
        costs,
        executor,
        RiskLimits(),
        EngineConfig(
            capital_usd=5000,
            max_positions=2,
            max_position_usd=1000,
            min_volume_usd=0,
            max_spread_bps=50,
            state_dir=str(tmp_path),
        ),
        clock=lambda: T0 / 1000,
    )
    eng.bootstrap()
    assert [c.describe().split()[0] for c in eng.candidates] == ["spot_perp"]

    eng.refresh(T0)  # prices are needed for the per-contract fee
    s = next(iter(eng.score_all().values()))
    # $0.20/contract on a $500 contract = 4 bps (> 2 bps rate); Kraken maker 40 bps; perp
    # taker leg also pays 5 bps slippage
    assert s.round_trip_cost == pytest.approx(2 * (0.004 + (0.20 / 500 + 0.0005)), rel=1e-3)
    assert s.capital_per_notional == pytest.approx(2.0)

    rep = eng.step()
    assert len(rep.opened) == 1
    pos = eng.positions[rep.opened[0]]
    assert pos.qty == pytest.approx(10_000)  # $1,000 / $0.10 -> 2 whole contracts
    assert venues["kraken"].fetch_spot_balance(doge) == pytest.approx(10_000)
    (perp,) = venues["coinbase"].fetch_perp_positions().values()
    assert perp.qty == pytest.approx(-10_000)
    assert pos.long_entry == pytest.approx(0.0999)  # bought at our resting bid
    assert polls  # the spot leg was worked passively
