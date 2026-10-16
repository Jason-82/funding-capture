"""Position and portfolio risk checks.

Each check returns alerts with a recommended action; the engine decides how to act.
The checks target the ways a "delta-neutral" carry trade actually loses money:

* one leg disappears or shrinks (liquidation, ADL, manual interference) -> naked exposure
* a perp leg drifts toward liquidation while the profit sits on the other venue
* the two legs are not the same thing (ticker collision, depeg) -> prices diverge
* stale or missing market data -> the bot is flying blind
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .models import PairPosition


class Action(IntEnum):  # ordered by severity
    NONE = 0
    WARN = 1
    REBALANCE = 2
    CLOSE = 3
    HALT = 4


@dataclass(frozen=True)
class RiskLimits:
    max_delta_drift: float = 0.02  # |long qty - short qty| / position qty -> rebalance
    close_delta_drift: float = 0.10  # beyond this something broke (ADL/liquidation) -> close
    warn_liq_distance: float = 0.35  # mark-to-liquidation distance (fraction) -> warn
    min_liq_distance: float = 0.20  # -> close before the venue does it for us
    warn_leg_divergence: float = 0.01  # leg mid prices disagree by this fraction -> warn
    max_leg_divergence: float = 0.03  # -> close (probably not the same asset, or a depeg)
    max_loss_frac: float = 0.03  # net P&L below -x * notional -> close
    max_data_age_s: float = 180.0  # quotes/funding older than this -> halt new entries


@dataclass(frozen=True)
class LegView:
    qty: float  # canonical units actually held on the venue, signed (+ long)
    mark: float | None
    liquidation_price: float | None = None


@dataclass(frozen=True)
class RiskAlert:
    action: Action
    check: str
    message: str
    position_id: str | None = None


def liq_distance(mark: float | None, liq: float | None) -> float | None:
    if not mark or not liq or liq <= 0:
        return None
    return abs(mark - liq) / mark


def check_position(
    pos: PairPosition, long: LegView, short: LegView, limits: RiskLimits
) -> list[RiskAlert]:
    alerts: list[RiskAlert] = []

    def add(action: Action, check: str, msg: str) -> None:
        alerts.append(RiskAlert(action, check, msg, pos.id))

    # 1. both legs present and matched. Spot balances may include coins the bot does not
    # own, so a spot leg only counts as short of target, never over it.
    held_long = min(long.qty, pos.qty) if not pos.candidate.long.is_perp else long.qty
    held_short = -short.qty
    if not pos.candidate.short.is_perp:
        held_short = min(-short.qty, pos.qty)
    drift = abs(held_long - held_short) / pos.qty if pos.qty > 0 else 0.0
    missing = max(pos.qty - held_long, pos.qty - held_short, 0.0) / pos.qty if pos.qty else 0.0
    if drift > limits.close_delta_drift or missing > limits.close_delta_drift:
        add(
            Action.CLOSE,
            "delta",
            f"legs badly mismatched: long {held_long:.6g} short {held_short:.6g} "
            f"(expected {pos.qty:.6g}); possible liquidation/ADL",
        )
    elif drift > limits.max_delta_drift or missing > limits.max_delta_drift:
        # also when both legs shrank alike (stopped mid-close): the record must shrink too
        add(Action.REBALANCE, "delta", f"delta drift {drift:.2%}, {missing:.2%} missing")

    # 2. liquidation distance on perp legs
    for name, leg, inst in (
        ("long", long, pos.candidate.long),
        ("short", short, pos.candidate.short),
    ):
        if not inst.is_perp:
            continue
        d = liq_distance(leg.mark, leg.liquidation_price)
        if d is None:
            continue
        if d < limits.min_liq_distance:
            add(Action.CLOSE, "liquidation", f"{name} leg {d:.1%} from liquidation")
        elif d < limits.warn_liq_distance:
            add(Action.WARN, "liquidation", f"{name} leg {d:.1%} from liquidation; add margin")

    # 3. legs priced as the same asset
    if long.mark and short.mark:
        div = abs(long.mark - short.mark) / ((long.mark + short.mark) / 2)
        if div > limits.max_leg_divergence:
            add(Action.CLOSE, "divergence", f"leg prices diverge {div:.2%}")
        elif div > limits.warn_leg_divergence:
            add(Action.WARN, "divergence", f"leg prices diverge {div:.2%}")

    # 4. stop loss on the whole pair
    if long.mark and short.mark and pos.qty > 0:
        unrealised = pos.qty * ((long.mark - pos.long_entry) - (short.mark - pos.short_entry))
        pnl = pos.funding_usd - pos.borrow_usd - pos.entry_fees_usd + unrealised
        if pnl < -limits.max_loss_frac * pos.entry_notional:
            add(Action.CLOSE, "loss", f"pair P&L ${pnl:,.2f} beyond stop")
    return alerts


def price_divergence(a: float | None, b: float | None) -> float | None:
    if not a or not b:
        return None
    return abs(a - b) / ((a + b) / 2)


def worst(alerts: list[RiskAlert]) -> Action:
    return max((a.action for a in alerts), default=Action.NONE)
