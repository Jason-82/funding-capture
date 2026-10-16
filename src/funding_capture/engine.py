"""The trading loop: refresh data -> account funding -> risk -> exits -> entries -> persist.

Runs identically against ``PaperVenue`` (simulated fills on live data) and ``CcxtVenue``
(real orders). Kill switches: create ``<state_dir>/STOP`` to block new entries, or
``<state_dir>/FLATTEN`` to close everything and stop opening.
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .costs import CostModel
from .data import HistoryStore, interval_for
from .execution import PairExecutor
from .forecast import StreamingForecaster
from .models import (
    MS_PER_HOUR,
    Candidate,
    FundingSnapshot,
    Instrument,
    PairPosition,
    PerpPositionInfo,
    Quote,
)
from .opportunities import (
    Scored,
    StrategyParams,
    build_candidates,
    passes_entry,
    score,
    should_exit,
)
from .risk import Action, LegView, RiskAlert, RiskLimits, check_position, price_divergence
from .state import load_json, position_from_dict, position_to_dict, save_json
from .venues.base import Venue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineConfig:
    capital_usd: float = 10_000.0
    max_positions: int = 5
    max_positions_per_asset: int = 1
    max_position_usd: float = 2_000.0  # notional cap per pair
    min_position_usd: float = 100.0
    max_venue_notional_usd: float | None = None  # default: capital_usd
    min_volume_usd: float = 5_000_000.0  # 24h volume required on *both* legs
    max_spread_bps: float = 15.0
    settlement_guard_minutes: float = 10.0
    risk_cooldown_minutes: float = 240.0  # no re-entry on instruments after a risk close
    funding_refresh_minutes: float = 10.0
    seed_history_days: int = 30
    include_assets: tuple[str, ...] = ()
    exclude_assets: tuple[str, ...] = ()
    state_dir: str = "state"
    loop_seconds: float = 60.0


@dataclass
class StepReport:
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    alerts: list[RiskAlert] = field(default_factory=list)
    top: list[Scored] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        venues: dict[str, Venue],
        strategy: StrategyParams,
        costs: CostModel,
        executor: PairExecutor,
        limits: RiskLimits,
        config: EngineConfig,
        *,
        clock=time.time,
        store: HistoryStore | None = None,
    ):
        self.venues = venues
        self.strategy = strategy
        self.costs = costs
        self.executor = executor
        self.limits = limits
        self.cfg = config
        self.clock = clock
        self.store = store
        self.forecaster = StreamingForecaster(strategy.forecast)
        self.positions: dict[str, PairPosition] = {}
        self.closed: list[PairPosition] = []
        self.instruments: dict[str, Instrument] = {}
        self.candidates: list[Candidate] = []
        self.snaps: dict[str, FundingSnapshot] = {}
        self.quotes: dict[str, Quote] = {}
        self.data_ms: dict[str, int] = {}
        self.entries_halted: str | None = None
        self._perp_positions: dict[str, PerpPositionInfo] = {}
        self._cooldown_until: dict[str, int] = {}  # instrument key -> ms
        self._venue_blocked_until: dict[str, int] = {}  # venue -> ms (account margin trouble)
        self._margin_low: set[str] = set()  # venues at/below the margin warning level
        # (position id, leg key) -> (rate, settlement ms) for venues without funding history
        self._est_pending: dict[tuple[str, str], tuple[float, int]] = {}
        self._funding_checked_ms = 0
        self._seq = 0
        self._started_ms: int | None = None  # first step ever (kept across restarts)
        self.state_path = Path(config.state_dir) / "engine.json"

    # ------------------------------------------------------------------ setup
    def bootstrap(self, load_state: bool = True) -> None:
        inc, exc = set(self.cfg.include_assets), set(self.cfg.exclude_assets)
        everything: list[Instrument] = []
        for name, venue in self.venues.items():
            if not getattr(venue, "can_trade", True):
                log.warning("%s: fills cannot be confirmed through its API; not trading it", name)
                continue
            for inst in venue.load_instruments():
                if (inc and inst.asset not in inc) or inst.asset in exc:
                    continue
                everything.append(inst)
        self.candidates = build_candidates(everything, self.strategy)
        # only track instruments that belong to some candidate (spots with no perp, etc. drop)
        self.instruments = {i.key: i for c in self.candidates for i in (c.long, c.short)}
        self._seed_forecaster()
        if load_state:
            self._load()
        log.info(
            "bootstrapped: %d instruments, %d candidates, %d open positions",
            len(self.instruments),
            len(self.candidates),
            len(self.positions),
        )

    def _seed_forecaster(self) -> None:
        if self.store is None:
            return
        cutoff = int(self.clock() * 1000) - self.cfg.seed_history_days * 24 * MS_PER_HOUR
        for inst in self.instruments.values():
            if not inst.is_perp:
                continue
            rows = [r for r in self.store.load_funding(inst) if r[0] >= cutoff]
            if rows:
                iv = interval_for(inst, rows)
                self.forecaster.seed(inst.key, [(ts, rate / iv) for ts, rate in rows])

    # ------------------------------------------------------------------ main loop
    def run(self, max_steps: int | None = None) -> None:
        self.bootstrap()
        n = 0
        while max_steps is None or n < max_steps:
            started = self.clock()
            try:
                rep = self.step()
                self._log_step(rep)
            except Exception:
                log.exception("step failed; continuing")
            n += 1
            time.sleep(max(0.0, self.cfg.loop_seconds - (self.clock() - started)))

    def step(self) -> StepReport:
        now = int(self.clock() * 1000)
        if self._started_ms is None:
            self._started_ms = now
        rep = StepReport()
        for v in self.venues.values():
            v.tick(now)
            v.heartbeat()
        self.refresh(now)
        self._account(now)
        self._kill_switches(rep, now)
        rep.alerts = self._risk(now)
        self._handle_alerts(rep, now)
        scored = self.score_all()
        rep.top = sorted(
            (s for s in scored.values() if not math.isnan(s.net_apr_on_capital)),
            key=lambda s: -s.net_apr_on_capital,
        )[:10]
        self._exits(scored, rep, now)
        self._entries(scored, rep, now)
        self._save()
        return rep

    # ------------------------------------------------------------------ data
    def _instruments_on(self, venue: str) -> list[Instrument]:
        return [i for i in self.instruments.values() if i.venue == venue]

    def refresh(self, now: int, with_positions: bool = True) -> None:
        held = {v for p in self.positions.values() for v in p.candidate.venues}

        def pull(name: str):
            v = self.venues[name]
            insts = self._instruments_on(name)
            perp_pos = v.fetch_perp_positions() if with_positions and name in held else {}
            return name, v.fetch_funding(insts), v.fetch_quotes(insts), perp_pos

        with ThreadPoolExecutor(max_workers=max(1, len(self.venues))) as pool:
            futures = {n: pool.submit(pull, n) for n in self.venues}
            for venue_name, fut in futures.items():
                try:
                    name, snaps, quotes, perp_pos = fut.result()
                except Exception as exc:
                    log.warning("%s: market data refresh failed: %s", venue_name, exc)
                    continue
                self.snaps.update(snaps)
                self.quotes.update(quotes)
                for k in [k for k in self._perp_positions if k.startswith(name + ":")]:
                    del self._perp_positions[k]
                self._perp_positions.update(perp_pos)
                self.data_ms[name] = now
                for key, snap in snaps.items():  # only fresh observations feed the forecast
                    self.forecaster.observe(key, snap.hourly, now)

    def _collect_funding(self, pos: PairPosition) -> None:
        """Add funding paid on the pair's perp legs since the last collection."""
        for inst in (pos.candidate.long, pos.candidate.short):
            if not inst.is_perp or not self.venues[inst.venue].reports_funding:
                continue
            since = pos.funding_cursor_ms.get(inst.key, pos.opened_ms - 1) + 1
            try:
                pays = self.venues[inst.venue].fetch_funding_payments(inst, since)
            except Exception as exc:
                log.warning("%s: funding fetch for %s failed: %s", pos.id, inst.key, exc)
                continue
            for ts, usd in pays:
                if ts >= since:
                    pos.funding_usd += usd
                    pos.funding_cursor_ms[inst.key] = max(
                        ts, pos.funding_cursor_ms.get(inst.key, 0)
                    )

    def _estimate_funding(self, pos: PairPosition, now: int) -> None:
        """For venues that can't report funding paid: credit each settlement at the rate last
        seen before it, on the position's size at mark (the same model as paper trading)."""
        for inst, sign in ((pos.candidate.long, 1), (pos.candidate.short, -1)):
            if not inst.is_perp or self.venues[inst.venue].reports_funding:
                continue
            key = (pos.id, inst.key)
            pending = self._est_pending.get(key)
            done = pos.funding_cursor_ms.get(inst.key, 0)
            if pending and now >= pending[1] > max(done, pos.opened_ms):
                mark = self._mark(inst) or pos.short_entry
                pos.funding_usd += -sign * pending[0] * pos.qty * mark
                pos.funding_cursor_ms[inst.key] = pending[1]
                if "funding estimated from published rates" not in pos.notes:
                    pos.notes.append("funding estimated from published rates")
            snap = self.snaps.get(inst.key)
            if snap is not None and snap.next_funding_ts:
                self._est_pending[key] = (snap.rate, int(snap.next_funding_ts))

    def _account(self, now: int) -> None:
        """Collect realised funding from venues and accrue borrow cost."""
        refresh = now - self._funding_checked_ms >= self.cfg.funding_refresh_minutes * 60_000
        for pos in self.positions.values():
            c = pos.candidate
            self._estimate_funding(pos, now)
            if refresh:
                self._collect_funding(pos)
            borrow_h = self.costs.borrow_hourly(c)
            if borrow_h:
                since = pos.borrow_accrued_ms or pos.opened_ms
                pos.borrow_usd += borrow_h * pos.entry_notional * (now - since) / MS_PER_HOUR
                pos.borrow_accrued_ms = now
        if refresh:
            self._funding_checked_ms = now

    def _mark(self, inst: Instrument) -> float | None:
        q = self.quotes.get(inst.key)
        if q is not None:
            return q.mid
        s = self.snaps.get(inst.key)
        return s.mark_price if s else None

    def _stale(self, venue: str, now: int) -> bool:
        return now - self.data_ms.get(venue, 0) > self.limits.max_data_age_s * 1000

    # ------------------------------------------------------------------ risk
    def _kill_switches(self, rep: StepReport, now: int) -> None:
        d = Path(self.cfg.state_dir)
        if (d / "FLATTEN").exists():
            self.entries_halted = "FLATTEN file present"
            for pos in list(self.positions.values()):
                self._close(pos, "kill switch: FLATTEN", rep, now, urgent=True)
        elif (d / "STOP").exists():
            self.entries_halted = "STOP file present"
        elif self.entries_halted and self.entries_halted.endswith("file present"):
            self.entries_halted = None

    def _leg_view(self, inst: Instrument) -> LegView:
        if inst.is_perp:
            p = self._perp_positions.get(inst.key)
            return LegView(
                p.qty if p else 0.0,
                self._mark(inst),
                p.liquidation_price if p else None,
            )
        return LegView(self.venues[inst.venue].fetch_spot_balance(inst), self._mark(inst))

    def _risk(self, now: int) -> list[RiskAlert]:
        alerts: list[RiskAlert] = []
        stale = [v for v in self.venues if self._stale(v, now)]
        if stale:
            alerts.append(RiskAlert(Action.HALT, "data", f"stale data from {', '.join(stale)}"))
        for pos in self.positions.values():
            c = pos.candidate
            if c.long.venue in stale or c.short.venue in stale:
                continue  # cannot judge it; the HALT alert already covers this
            alerts.extend(
                check_position(pos, self._leg_view(c.long), self._leg_view(c.short), self.limits)
            )
        alerts.extend(self._account_margin_alerts(stale, now))
        return alerts

    def _account_margin_alerts(self, stale: list[str], now: int) -> list[RiskAlert]:
        """Venues that report margin per account (Coinbase US futures) rather than a
        liquidation price per position: act on the account-wide buffer, and stop opening
        anything new on an account that is short of margin."""
        alerts: list[RiskAlert] = []
        self._margin_low.clear()
        held = {v for p in self.positions.values() for v in p.candidate.venues}
        for name in held - set(stale):
            buf = self.venues[name].margin_buffer()
            if buf is None:
                continue
            if buf < self.limits.warn_liq_distance:
                self._margin_low.add(name)
            if buf < self.limits.min_liq_distance:
                until = now + int(self.cfg.risk_cooldown_minutes * 60_000)
                self._venue_blocked_until[name] = until
                action, msg = Action.CLOSE, f"{name} account {buf:.1%} from liquidation"
            elif buf < self.limits.warn_liq_distance:
                action, msg = Action.WARN, f"{name} account {buf:.1%} from liquidation; add margin"
            else:
                continue
            for pos in self.positions.values():
                if name in pos.candidate.venues:
                    alerts.append(RiskAlert(action, "liquidation", msg, pos.id))
        return alerts

    def _handle_alerts(self, rep: StepReport, now: int) -> None:
        data_halt = any(a.action is Action.HALT for a in rep.alerts)
        if data_halt:
            self.entries_halted = "stale market data"
        elif self.entries_halted == "stale market data":
            self.entries_halted = None
        by_pos: dict[str, list[RiskAlert]] = {}
        for a in rep.alerts:
            level = logging.WARNING if a.action >= Action.WARN else logging.INFO
            log.log(level, "risk %s [%s] %s: %s", a.action.name, a.check, a.position_id, a.message)
            if a.position_id:
                by_pos.setdefault(a.position_id, []).append(a)
        for pid, alerts in by_pos.items():
            pos = self.positions.get(pid)
            if pos is None:
                continue
            action = max(a.action for a in alerts)
            if action is Action.CLOSE:
                closes = [a for a in alerts if a.action is Action.CLOSE]
                why = "risk: " + "; ".join(a.message for a in closes)
                until = now + int(self.cfg.risk_cooldown_minutes * 60_000)
                for inst in (pos.candidate.long, pos.candidate.short):
                    self._cooldown_until[inst.key] = until
                if any(a.check == "delta" for a in closes):
                    self._flatten(pos, why, rep, now)  # a leg is gone: can't pair-trade it
                else:
                    self._close(pos, why, rep, now, urgent=True)
            elif action is Action.REBALANCE:
                self._rebalance(pos)

    # ------------------------------------------------------------------ scoring
    def _leg_rates(self, inst: Instrument) -> tuple[float, float]:
        if not inst.is_perp:
            return 0.0, 0.0
        snap = self.snaps.get(inst.key)
        if snap is None:
            return math.nan, math.nan
        fc = self.forecaster.forecast(inst.key, self.strategy.horizon_hours, snap.hourly)
        return snap.hourly, fc if fc is not None else math.nan

    def _score(self, c: Candidate) -> Scored:
        ln, lf = self._leg_rates(c.long)
        sn, sf = self._leg_rates(c.short)
        return score(
            c, ln, sn, lf, sf, self.costs, self.strategy, self._mark(c.long), self._mark(c.short)
        )

    def score_all(self) -> dict[str, Scored]:
        return {c.key: self._score(c) for c in self.candidates}

    def _imminent_funding(self, c: Candidate, now: int) -> float:
        """Funding the pair receives (per $) at settlements inside the guard window."""
        guard = self.cfg.settlement_guard_minutes * 60_000
        total = 0.0
        for inst, sign in ((c.long, 1), (c.short, -1)):
            snap = self.snaps.get(inst.key) if inst.is_perp else None
            if snap and snap.next_funding_ts and 0 <= snap.next_funding_ts - now <= guard:
                total += -sign * snap.rate
        return total

    # ------------------------------------------------------------------ exits
    def _exits(self, scored: dict[str, Scored], rep: StepReport, now: int) -> None:
        for pos in list(self.positions.values()):
            s = scored.get(pos.candidate.key) or self._score(pos.candidate)
            reason = should_exit(s, self.strategy, (now - pos.opened_ms) / MS_PER_HOUR)
            if reason is None:
                continue
            if self._imminent_funding(pos.candidate, now) > 0:
                continue  # collect the settlement that is about to pay us, then leave
            self._close(pos, reason, rep, now)

    def _close(
        self, pos: PairPosition, reason: str, rep: StepReport, now: int, urgent: bool = False
    ) -> None:
        c = pos.candidate
        try:
            ex = self.executor.close(c, pos.qty, urgent=urgent)
        except Exception as exc:  # the executor reports errors; this is a last resort
            self.entries_halted = f"close of {pos.id} raised: {exc}"
            log.critical("close %s raised; exposure unknown, entries halted: %s", pos.id, exc)
            pos.notes.append(f"close failed: {exc}")
            return
        sold = ex.qty(c.long, "sell")
        bought = ex.qty(c.short, "buy")
        done = min(sold, bought)
        pos.exit_fees_usd += ex.fees_usd
        pos.long_exit = ex.avg_price(c.long, "sell") or pos.long_exit
        pos.short_exit = ex.avg_price(c.short, "buy") or pos.short_exit
        if pos.long_exit and pos.short_exit:
            pos.realized_price_usd += done * (
                (pos.long_exit - pos.long_entry) - (pos.short_exit - pos.short_entry)
            )
        for m in ex.messages:
            pos.notes.append(m)
            log.warning("%s close: %s", pos.id, m)
        if ex.has_residual or ex.unsafe:
            self.entries_halted = f"unhedged residual or execution error closing {pos.id}"
            log.critical("%s: residual %s, messages %s", pos.id, ex.residual, ex.messages)
        if done >= pos.qty * 0.999:
            self._collect_funding(pos)
            pos.closed_ms = now
            pos.notes.append(f"closed: {reason}")
            self.closed.append(self.positions.pop(pos.id))
            for inst in (c.long, c.short):
                if inst.is_perp:
                    self._sweep_dust(inst, pos)
            rep.closed.append(pos.id)
            log.info("CLOSED %s (%s) net $%.2f", pos.id, reason, pos.net_pnl_usd)
        else:
            pos.qty -= done
            pos.notes.append(f"partial close {done:.6g}: {reason}")

    def _flatten(self, pos: PairPosition, reason: str, rep: StepReport, now: int) -> None:
        """Close each leg on its own, for what is actually held (used when legs mismatch)."""
        c = pos.candidate
        flat = True
        for inst, sign in ((c.long, 1), (c.short, -1)):
            view = self._leg_view(inst)
            held = sign * view.qty if inst.is_perp else min(sign * view.qty, pos.qty)
            if held <= 0:
                continue
            side = "sell" if sign > 0 else "buy"
            try:
                f = self.executor.trade_one(inst, side, held, reduce_only=inst.is_perp)
            except Exception as exc:
                log.error("flatten %s %s failed: %s", pos.id, inst.key, exc)
                flat = False
                continue
            pos.exit_fees_usd += f.fee_usd
            entry = pos.long_entry if sign > 0 else pos.short_entry
            pos.realized_price_usd += sign * f.qty * (f.price - entry)
            if f.qty < held * 0.999:
                flat = False
        if flat:
            self._collect_funding(pos)
            pos.closed_ms = now
            pos.notes.append(f"flattened: {reason}")
            self.closed.append(self.positions.pop(pos.id))
            rep.closed.append(pos.id)
            log.warning("FLATTENED %s (%s)", pos.id, reason)
        else:
            self.entries_halted = f"could not flatten {pos.id}"
            log.critical("could not flatten %s; manual attention needed", pos.id)

    def _sweep_dust(self, inst: Instrument, pos: PairPosition) -> None:
        """Flatten what is left on a perp after a close (lot rounding, fee-in-coin drift).

        Safe because the engine never shares an instrument between two open pairs.
        """
        try:
            left = self.venues[inst.venue].fetch_perp_positions().get(inst.key)
            if left is None or left.qty == 0:
                return
            side = "sell" if left.qty > 0 else "buy"
            f = self.executor.trade_one(inst, side, abs(left.qty), reduce_only=True)
            pos.exit_fees_usd += f.fee_usd
        except Exception as exc:
            log.warning("dust sweep on %s failed: %s", inst.key, exc)

    def _rebalance(self, pos: PairPosition) -> None:
        """Trim the larger leg down to the smaller one (reduces exposure, never adds)."""
        c = pos.candidate
        lv, sv = self._leg_view(c.long), self._leg_view(c.short)
        held_long = lv.qty if c.long.is_perp else min(lv.qty, pos.qty)
        held_short = -sv.qty if c.short.is_perp else min(-sv.qty, pos.qty)
        diff = held_long - held_short
        try:
            if diff > 0:
                f = self.executor.trade_one(c.long, "sell", diff, c.long.is_perp)
            else:
                f = self.executor.trade_one(c.short, "buy", -diff, c.short.is_perp)
        except Exception as exc:
            log.error("rebalance %s failed: %s", pos.id, exc)
            return
        pos.exit_fees_usd += f.fee_usd
        if diff > 0:
            held_long -= f.qty
        else:
            held_short -= f.qty
        pos.qty = min(held_long, held_short)
        pos.notes.append(f"rebalanced: trimmed {f.qty:.6g} ({'long' if diff > 0 else 'short'})")

    # ------------------------------------------------------------------ entries
    def _entries(self, scored: dict[str, Scored], rep: StepReport, now: int) -> None:
        if self.entries_halted:
            return
        cfg = self.cfg
        if len(self.positions) >= cfg.max_positions:
            return
        used_insts = {
            i.key for p in self.positions.values() for i in (p.candidate.long, p.candidate.short)
        }
        per_asset: dict[str, int] = {}
        venue_notional: dict[str, float] = {}
        used_capital = 0.0
        for p in self.positions.values():
            per_asset[p.candidate.asset] = per_asset.get(p.candidate.asset, 0) + 1
            for v in p.candidate.venues:
                venue_notional[v] = venue_notional.get(v, 0.0) + p.entry_notional
            used_capital += p.entry_notional * self.costs.capital_per_notional(p.candidate)
        venue_cap = cfg.max_venue_notional_usd or cfg.capital_usd

        ranked = sorted(
            (s for s in scored.values() if passes_entry(s, self.strategy)),
            key=lambda s: -s.net_apr_on_capital,
        )
        for s in ranked:
            if len(self.positions) >= cfg.max_positions or self.entries_halted:
                break  # an execution in this loop may have just halted entries
            c = s.candidate
            if per_asset.get(c.asset, 0) >= cfg.max_positions_per_asset:
                continue
            if c.long.key in used_insts or c.short.key in used_insts:
                continue
            if max(self._cooldown_until.get(i.key, 0) for i in (c.long, c.short)) > now:
                continue
            if any(
                v in self._margin_low or self._venue_blocked_until.get(v, 0) > now for v in c.venues
            ):
                continue  # that account is short of margin: add none, let it recover
            if not self.tradeable(c, now):
                continue
            if self._imminent_funding(c, now) < 0:
                continue  # wait until the settlement we would pay has passed
            budget = min(cfg.capital_usd / cfg.max_positions, cfg.capital_usd - used_capital)
            notional = min(cfg.max_position_usd, budget / s.capital_per_notional)
            if any(venue_notional.get(v, 0.0) + notional > venue_cap for v in c.venues):
                continue
            if notional < cfg.min_position_usd:
                break
            pos = self._open(s, notional, now)
            if pos is None:
                continue
            rep.opened.append(pos.id)
            per_asset[c.asset] = per_asset.get(c.asset, 0) + 1
            used_insts |= {c.long.key, c.short.key}
            for v in c.venues:
                venue_notional[v] = venue_notional.get(v, 0.0) + notional
            used_capital += notional * s.capital_per_notional

    def tradeable(self, c: Candidate, now: int) -> bool:
        ql, qs = self.quotes.get(c.long.key), self.quotes.get(c.short.key)
        if ql is None or qs is None:
            return False
        if self._stale(c.long.venue, now) or self._stale(c.short.venue, now):
            return False
        for q, maker in zip((ql, qs), self.costs.maker_legs(c), strict=True):
            if (q.volume_usd_24h or 0.0) < self.cfg.min_volume_usd:
                return False
            # only the leg worked with post-only orders joins the bid/ask; the other pays it
            if q.spread_bps > self.cfg.max_spread_bps * (4 if maker else 1):
                return False
        div = price_divergence(ql.mid, qs.mid)
        return div is not None and div <= self.limits.warn_leg_divergence

    def _open(self, s: Scored, notional: float, now: int) -> PairPosition | None:
        c = s.candidate
        try:
            ex = self.executor.open(c, notional)
        except Exception as exc:  # the executor reports errors; this is a last resort
            self.entries_halted = f"open of {c.key} raised: {exc}"
            log.critical("open %s raised; exposure unknown, entries halted: %s", c.key, exc)
            return None
        for m in ex.messages:
            log.warning("open %s: %s", c.key, m)
        if ex.has_residual or ex.unsafe:
            self.entries_halted = f"unhedged residual or execution error opening {c.key}"
            log.critical("after open %s: residual %s, messages %s", c.key, ex.residual, ex.messages)
        long_px, short_px = ex.avg_price(c.long, "buy"), ex.avg_price(c.short, "sell")
        qty = min(
            ex.qty(c.long, "buy") - ex.qty(c.long, "sell"),
            ex.qty(c.short, "sell") - ex.qty(c.short, "buy"),
        )
        if qty <= 0 or long_px is None or short_px is None:
            log.info("open %s: nothing hedged (%s)", c.key, "; ".join(ex.messages))
            return None
        self._seq += 1
        pos = PairPosition(
            id=f"P{now // 1000}-{self._seq}",
            candidate=c,
            qty=qty,
            long_entry=long_px,
            short_entry=short_px,
            opened_ms=now,
            entry_fees_usd=ex.fees_usd,
            notes=[f"opened at {s.net_apr_on_capital:.1%} expected net APR"] + ex.messages,
        )
        self.positions[pos.id] = pos
        self._estimate_funding(pos, now)  # note the upcoming settlement for estimating venues
        log.info(
            "OPENED %s %s qty=%.6g notional=$%.0f exp.APR=%.1f%%",
            pos.id,
            c.describe(),
            qty,
            pos.entry_notional,
            s.net_apr_on_capital * 100,
        )
        return pos

    # ------------------------------------------------------------------ persistence
    def _save(self) -> None:
        data = {
            "positions": [position_to_dict(p) for p in self.positions.values()],
            "closed": [position_to_dict(p) for p in self.closed[-500:]],
            "forecaster": self.forecaster.to_dict(),
            "seq": self._seq,
            "started_ms": self._started_ms,
            "capital_usd": self.cfg.capital_usd,
            "venues": {n: v.to_dict() for n, v in self.venues.items() if hasattr(v, "to_dict")},
        }
        save_json(self.state_path, data)

    def _load(self) -> None:
        data = load_json(self.state_path)
        if not data:
            return
        self.positions = {d["id"]: position_from_dict(d) for d in data.get("positions", [])}
        self.closed = [position_from_dict(d) for d in data.get("closed", [])]
        self.forecaster.load_dict(data.get("forecaster", {}))
        self._seq = data.get("seq", 0)
        self._started_ms = data.get("started_ms")
        for n, vd in (data.get("venues") or {}).items():
            v = self.venues.get(n)
            if v is not None and hasattr(v, "load_dict"):
                v.load_dict(vd)

    # ------------------------------------------------------------------ reporting
    def summary(self) -> str:
        lines = [f"open positions: {len(self.positions)}  closed: {len(self.closed)}"]
        if self.entries_halted:
            lines.append(f"ENTRIES HALTED: {self.entries_halted}")
        for p in self.positions.values():
            lines.append(
                f"  {p.id} {p.candidate.describe()} qty={p.qty:.6g} "
                f"funding=${p.funding_usd:,.2f} fees=${p.entry_fees_usd + p.exit_fees_usd:,.2f}"
            )
        realised = sum(p.net_pnl_usd for p in self.closed)
        lines.append(f"realised net P&L on closed pairs: ${realised:,.2f}")
        return "\n".join(lines)

    def _log_step(self, rep: StepReport) -> None:
        if rep.top:
            best = rep.top[0]
            log.info(
                "best: %s net APR %.1f%% (gross now %.1f%%)",
                best.candidate.describe(),
                best.net_apr_on_capital * 100,
                best.gross_apr_now * 100,
            )
        log.info(self.summary())
