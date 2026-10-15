"""Trading-cost and capital model.

All rates are fractions (0.0005 == 5 bps). Fee defaults are base-tier public schedules
as of writing and *will* drift: override them in config with your actual tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Candidate, Construction, Instrument, Kind


@dataclass(frozen=True)
class VenueFees:
    spot_taker: float = 0.0010
    spot_maker: float = 0.0010
    perp_taker: float = 0.0006
    perp_maker: float = 0.0002
    # US futures charge per contract: each perp fill costs at least this much (USD) per
    # contract. Applied when a price is known (scanner/engine); the backtest ignores it.
    perp_min_fee_per_contract: float = 0.0

    def fee(self, kind: Kind, taker: bool = True) -> float:
        if kind is Kind.SPOT:
            return self.spot_taker if taker else self.spot_maker
        return self.perp_taker if taker else self.perp_maker

    def fill_fee_usd(self, inst: Instrument, qty: float, price: float, taker: bool) -> float:
        """Fee for one fill, including the per-contract minimum on perps."""
        fee = qty * price * self.fee(inst.kind, taker)
        if inst.is_perp and self.perp_min_fee_per_contract:
            contracts = qty / (inst.contract_size * inst.multiplier)
            fee = max(fee, contracts * self.perp_min_fee_per_contract)
        return fee


# Base-tier (no VIP, no token discount) schedules. Verify before trusting.
DEFAULT_FEES: dict[str, VenueFees] = {
    "binance": VenueFees(
        spot_taker=0.0010, spot_maker=0.0010, perp_taker=0.0005, perp_maker=0.0002
    ),
    "bybit": VenueFees(spot_taker=0.0010, spot_maker=0.0010, perp_taker=0.00055, perp_maker=0.0002),
    "okx": VenueFees(spot_taker=0.0010, spot_maker=0.0008, perp_taker=0.0005, perp_maker=0.0002),
    "bitget": VenueFees(spot_taker=0.0010, spot_maker=0.0010, perp_taker=0.0006, perp_maker=0.0002),
    "gate": VenueFees(spot_taker=0.0010, spot_maker=0.0010, perp_taker=0.0005, perp_maker=0.0002),
    "hyperliquid": VenueFees(
        spot_taker=0.0007, spot_maker=0.0004, perp_taker=0.00045, perp_maker=0.00015
    ),
    "dydx": VenueFees(spot_taker=0.0, spot_maker=0.0, perp_taker=0.0005, perp_maker=0.0001),
    "lighter": VenueFees(spot_taker=0.0, spot_maker=0.0, perp_taker=0.0, perp_maker=0.0),
    # US venues. Coinbase Advanced "Intro" tier since 2026-09-16 (0.50% / 0.90%). Its US
    # perpetual-style futures charge per contract: $0.10 exchange fee plus an unpublished
    # FCM commission, modelled as max(0.02%, $0.20) per contract per side until you
    # confirm yours with an order preview.
    "coinbase": VenueFees(
        spot_taker=0.0090,
        spot_maker=0.0050,
        perp_taker=0.0002,
        perp_maker=0.0002,
        perp_min_fee_per_contract=0.20,
    ),
    # Kraken Pro Tier 1 after the July 2026 schedule change (unconfirmed; earlier it was
    # 0.25% / 0.40%). `fcap check` prints your real rate once API keys are set.
    "kraken": VenueFees(spot_taker=0.0080, spot_maker=0.0040, perp_taker=0.0005, perp_maker=0.0002),
}


@dataclass
class CostModel:
    fees: dict[str, VenueFees] = field(default_factory=lambda: dict(DEFAULT_FEES))
    default_fees: VenueFees = field(default_factory=VenueFees)
    slippage_bps: float = 3.0  # per fill, on top of fees (half-spread + impact)
    taker: bool = True  # assume aggressive fills everywhere except maker_venues
    # Legs on these venues are worked with post-only orders: maker fee, no slippage.
    maker_venues: tuple[str, ...] = ()
    borrow_apr: float = 0.10  # assumed margin-borrow cost for short-spot legs
    perp_leverage: float = 2.0  # notional / margin posted on each perp leg
    spot_short_margin: float = 0.5  # margin posted per $ of borrowed spot sold short
    # Long-spot collateral counted toward the short perp's margin on the same venue
    # (portfolio / unified margin). 0 = separate wallets, 1 = fully cross-margined.
    unified_margin_credit: float = 0.0

    def maker_legs(self, cand: Candidate) -> tuple[bool, bool]:
        """Which legs the executor works passively: legs on maker venues, but only one per
        pair (the spot leg when both qualify), mirroring ``PairExecutor._maker_leg``."""
        ml, ms = cand.long.venue in self.maker_venues, cand.short.venue in self.maker_venues
        if ml and ms:
            ml = cand.long.kind is Kind.SPOT
            ms = not ml
        return ml, ms

    def leg_cost(
        self, inst: Instrument, price: float | None = None, maker: bool | None = None
    ) -> float:
        """Cost fraction of one fill (one side, one leg). ``price``: USD per asset unit."""
        fees = self.fees.get(inst.venue, self.default_fees)
        if maker is None:
            maker = inst.venue in self.maker_venues
        fee = fees.fee(inst.kind, taker=not maker and self.taker)
        if inst.is_perp and fees.perp_min_fee_per_contract and price:
            contract_value = inst.contract_size * inst.multiplier * price
            fee = max(fee, fees.perp_min_fee_per_contract / contract_value)
        return fee if maker else fee + self.slippage_bps / 1e4

    def round_trip_cost(
        self, cand: Candidate, long_price: float | None = None, short_price: float | None = None
    ) -> float:
        """Fraction of notional spent to open *and* close both legs."""
        ml, ms = self.maker_legs(cand)
        return 2 * (
            self.leg_cost(cand.long, long_price, ml) + self.leg_cost(cand.short, short_price, ms)
        )

    def capital_per_notional(self, cand: Candidate) -> float:
        """USD of capital tied up per USD of hedged notional."""
        perp_margin = 1.0 / self.perp_leverage
        if cand.construction is Construction.SPOT_PERP:
            capital = 1.0 + perp_margin
            if cand.long.venue == cand.short.venue:
                capital -= perp_margin * self.unified_margin_credit
            return capital
        if cand.construction is Construction.REVERSE:
            return self.spot_short_margin + perp_margin
        return 2 * perp_margin

    def borrow_hourly(self, cand: Candidate) -> float:
        if cand.construction is Construction.REVERSE:
            return self.borrow_apr / (24 * 365)
        return 0.0
