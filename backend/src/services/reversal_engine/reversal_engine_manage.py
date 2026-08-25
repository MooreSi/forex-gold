"""TP/SL/partial-close management for triggered Reversal Engine signals -- extracted
verbatim (no logic changes) from engine.py's _check_outcomes as part of
task 040. See docs/todo/refactor/backend-foundation/040-*.md.

_ManagementMixin is composed into ReversalEngine (reversal_engine_service.py)
-- these methods rely on `self` attributes (_main_eng, _live_missing_streak,
_notify_refresh) defined there, exactly as they did when this code lived
inline in engine.py.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from backend.src.utils.models import STRATEGY_CONSERVATIVE
from backend.src.services.reversal_engine import reversal_engine_repo as re_db

_log = logging.getLogger("reversal_engine")

_LIVE_MISSING_THRESHOLD = 3   # consecutive get_positions() misses before treating a live ticket as closed

# Lot size for virtual P&L tracking (not live yet)
_VIRTUAL_LOT = 0.1

# GD2/Institutional signals are identified by source_channel, not strategy --
# sig["strategy"] is overwritten with whatever real STRATEGY_* the "Reversal Engine
# Engine" channel override (or the global default) currently resolves to
# (see reversal_engine_service._run_cycle: `sig_data["strategy"] =
# self._active_strategy()`, applied AFTER signal_generator.build_signal()
# tags it "gd2_unicorn"/"signal_climber"). "gd2_unicorn" is not a real
# STRATEGY_* name, so a check against it here can never match -- every GD2
# signal was silently falling through to the REF 8-level ladder branch below,
# where its missing tp4/tp7 fields resolve via the `or` fallbacks to tp3 (as
# tp_mid) and tp1 (as tp7), closing the runner early instead of riding it to
# calculate_gd2_tp_structure's actual 6R target.
_GD2_SOURCE_CHANNEL = "GOLD DIGGERS 2.0 ⚡️"  # matches signal_generator.build_signal

# Partial-close ladder fractions for the REF-style/GD2 ladder -- retuned
# from the original 33/33/34 split (2026-07-16 Reversal Engine improvements pack).
# The fork's real trade history shows a 72% win rate that's still net
# losing: most trades only ever reach TP1 before reversing back out at the
# BE stop, and at 33% that leg doesn't bank enough to outweigh a full SL
# loss (0% banked, the entire sl_dist risked) -- sl_distance_for_level is
# 4-7pts against TP1's fixed 3pt offset, already an unfavourable R:R on
# the first leg alone, so the loss:win $ ratio stays lopsided even at a
# 72% hit rate. Front-loading more onto the leg that's actually reached
# most reliably is a reasoned rebalance, NOT a backtested optimum -- only
# ~25 real trades exist so far on this fork, nowhere near enough to fit
# these numbers rigorously (unlike score_level()'s type weights, which
# were fit against 767 real signals). Revisit once there's a larger
# real sample.
_TP1_FRAC   = 0.50
_MID_FRAC   = 0.30
_FINAL_FRAC = 0.20   # implicit: whatever remains rides to the final target
_POST_TP1_REMAINING = round(1.0 - _TP1_FRAC, 4)                 # remaining right after TP1 (0.50)
_POST_MID_REMAINING = round(_POST_TP1_REMAINING - _MID_FRAC, 4)  # remaining right after mid (0.20)

# STRATEGY_CONSERVATIVE (see core/models.py's STRATEGY_DESCRIPTIONS): fixed
# SL/TP1 from the actual fill price, signal levels discarded entirely, 80%
# booked at TP1, then a tight trail on the runner. Modelled here so the
# virtual ML labels for a signal reflect the SAME management style that
# would actually be applied to a real trade under this setting, rather than
# the REF-ladder model built for the (different) default/scale_out style.
_CONSERVATIVE_SL_PTS    = 5.0
_CONSERVATIVE_TP1_PTS   = 3.0
_CONSERVATIVE_TP1_FRAC  = 0.80
_CONSERVATIVE_TRAIL_PTS = 3.0

# Slippage allowed BEYOND the stop price when booking a stop-out, on top of the
# spread. Anything past this is polling latency (the outcome loop runs every
# 5s), not slippage, and charging it to the trade is what pushed the average
# loss to -1.263R against a 1R stop. See _stop_fill.
_STOP_SLIP_MAX_PTS = 0.5


class _ManagementMixin:
    def _realistic_fill(self, tick, direction: str, closing: bool) -> float:
        """Approximate a real MT5 fill price: buys fill at ask, sells at bid.
        closing=True means we're exiting the position (opposite side of entry)."""
        buy_side = (direction == "BUY") != closing
        return float(tick.ask if buy_side else tick.bid) or float(tick.mid or 0)

    def _stop_fill(self, tick, direction: str, sl: float) -> float:
        """Where a stop-out actually fills — at the stop, not wherever the poll
        caught price.

        The outcome loop runs every _OUTCOME_INTERVAL_S (5s), and the SL branch
        used to book the exit at the CURRENT tick. So every point gold happened
        to travel between the stop being touched and the next poll was charged
        to the trade, as if the position had sat there unprotected. A broker
        stop does not behave that way: it triggers at the stop and fills within
        slippage of it.

        Measured across 605 closed losing signals, 84% came in worse than -1.0R
        (worst -5.75R), which is what dragged the average loss to -1.263R
        against +0.406R average wins -- a 0.32:1 payoff that needs a 75.7% win
        rate to break even, against the 70.5% actually achieved. The model then
        trains on those labels, so it has been learning that these trades lose
        by more than a stopped-out trade really loses.

        Slippage is allowed but bounded: the spread (the real cost of crossing)
        plus _STOP_SLIP_MAX_PTS. Beyond that is polling latency, not slippage.
        The fill is also never better than the stop itself -- once touched, a
        stop is filled, so a price that recovered before the next poll must not
        hand the trade a better exit than it actually got.
        """
        raw = self._realistic_fill(tick, direction, closing=True)
        try:
            spread = abs(float(tick.ask) - float(tick.bid))
        except (TypeError, ValueError):
            spread = 0.0
        allowance = spread + _STOP_SLIP_MAX_PTS
        if direction == "BUY":
            # Stop sits below entry; adverse is lower.
            return min(float(sl), max(raw, float(sl) - allowance))
        # Stop sits above entry; adverse is higher.
        return max(float(sl), min(raw, float(sl) + allowance))

    def _net_pnl(self, sig: dict, pnl_pts: float, exit_tick) -> tuple[float, float]:
        """(gross_pnl_dollars, net_pnl_dollars) after spread/commission/slippage,
        using the same fee model as real the reference channel trades on the main engine."""
        gross = pnl_pts * _VIRTUAL_LOT * 100
        if self._main_eng is None:
            return gross, gross
        try:
            hold_hours = (time.time() - float(sig.get("trigger_time") or sig.get("created_at", time.time()))) / 3600.0
            spread = float(getattr(exit_tick, "spread", 0.0) or 0.0)
            costs = self._main_eng.calculate_fees(_VIRTUAL_LOT, spread, hold_hours)
            net = gross - costs["total_cost"]
            return round(gross, 4), round(net, 4)
        except Exception:
            return gross, gross

    async def _manage_triggered_signal(self, sig: dict, tick) -> None:
        """Dispatches to the management model matching this signal's actual
        source/strategy -- a triggered, NOT live-managed signal (see
        _check_outcomes: live-executed signals are routed to
        _reconcile_live_signal instead, never here) can be either a GD2/
        Unicorn setup (own tp1/tp2/tp3 mapping, see below) or a REF-style
        signal, whose virtual management should itself vary by whichever
        STRATEGY_* is actually configured for the "Reversal Engine" channel
        (see _active_strategy) so the ML labels reflect what a real trade
        under that setting would have done."""
        if sig.get("source_channel") == _GD2_SOURCE_CHANNEL:
            await self._manage_gd2_signal(sig, tick)
        elif sig.get("strategy") == STRATEGY_CONSERVATIVE:
            await self._manage_conservative_signal(sig, tick)
        else:
            await self._manage_ref_ladder_signal(sig, tick)

    async def _manage_gd2_signal(self, sig: dict, tick) -> None:
        """GD2/Unicorn signals only ever carry tp1/tp2/tp3 (see
        signal_generator.calculate_gd2_tp_structure -- 1R partial, 2R
        partial+BE, 6R runner), not the REF 8-level ladder -- reuses the
        same ladder mechanics as _manage_ref_ladder_signal with tp2/tp3
        mapped onto the mid/final slots instead of tp4/tp7."""
        await self._manage_ref_ladder_signal(
            sig, tick, tp_mid_field="tp2", tp7_field="tp3", tp_mid_idx=2)

    async def _manage_ref_ladder_signal(self, sig: dict, tick,
                                        tp_mid_field: str = "tp4", tp7_field: str = "tp7",
                                        tp_mid_idx: int = 4) -> None:
        """REF-style ladder scale-out for the default/scale_out family of
        strategies -- uses the signal's own SL/TP levels as sent.

        The old logic moved SL to raw entry at TP1 and only counted a "win"
        at TP7 -- 88 of 118 TP1-hitting signals closed 'be' with $0 banked,
        tanking both the reported win rate and the ML labels. the reference channel itself
        calls TP1 a win; we bank _TP1_FRAC there, _MID_FRAC at the
        mid-ladder TP, and let _FINAL_FRAC ride to TP7 (see the module-level
        constants' docstring for why this is 50/30/20, not the original
        33/33/34)."""
        sig_id     = sig["id"]
        direction  = sig["direction"]
        sl         = sig["stop_loss"]
        tp1        = sig["tp1"]
        tp_mid = sig.get(tp_mid_field) or sig.get("tp3") or tp1   # mid-ladder partial
        tp7    = sig.get(tp7_field) or sig.get("tp8") or tp1
        entry_lo  = sig["entry_low"]
        entry_hi  = sig["entry_high"]
        entry_mid = (entry_lo + entry_hi) / 2
        entry_ref = float(sig.get("trigger_price") or entry_mid)  # realistic fill once triggered
        remaining = float(sig.get("remaining_frac") if sig.get("remaining_frac") is not None else 1.0)
        partial_booked = float(sig.get("partial_pnl_dollars") or 0.0)
        price = float(tick.mid or tick.bid or 0)

        # How far this signal actually travelled each way, recorded while it is
        # live because nothing else captures it: max_tp_hit only says which
        # fixed target was tagged, not how close the others came. Without it,
        # any change to stop width or target distance is a guess -- and the
        # obvious guess ("raise TP1 above 1R") is measurably wrong here, since
        # only 9.4% of signals ever reach 1.0R. See the migration note in
        # reversal_engine/database.py.
        try:
            _fav = (price - entry_ref) if direction == "BUY" else (entry_ref - price)
            re_db.record_excursion(sig_id, _fav, -_fav)
        except Exception:
            pass

        if direction == "BUY":
            hit_sl    = price <= sl
            hit_tp1   = price >= tp1 and remaining >= 1.0 - 1e-6
            hit_mid   = (tp_mid and price >= float(tp_mid)
                         and _POST_MID_REMAINING < remaining <= _POST_TP1_REMAINING + 1e-6)
            hit_final = tp7 and price >= float(tp7)
        else:
            hit_sl    = price >= sl
            hit_tp1   = price <= tp1 and remaining >= 1.0 - 1e-6
            hit_mid   = (tp_mid and price <= float(tp_mid)
                         and _POST_MID_REMAINING < remaining <= _POST_TP1_REMAINING + 1e-6)
            hit_final = tp7 and price <= float(tp7)

        # Round-trip cost in points for BE placement (spread + slippage).
        _cost_pts = round((float(tick.ask) - float(tick.bid)) + 0.05, 2)

        def _leg_pts(exit_px: float) -> float:
            return (exit_px - entry_ref) if direction == "BUY" else (entry_ref - exit_px)

        if hit_sl:
            # At the stop, not at whatever tick the 5s poll happened to catch.
            exit_fill = self._stop_fill(tick, direction, sl)
            leg_pts   = _leg_pts(exit_fill)
            gross_leg, net_leg = self._net_pnl(sig, leg_pts, tick)
            gross_leg *= remaining
            net_leg   *= remaining
            total_net = round(partial_booked + net_leg, 2)
            # Outcome by TOTAL realized result -- a trade that banked TP1
            # partials before the BE stop is a win, matching REF accounting.
            if total_net > 0.5:
                outcome = "win"
            elif total_net < -0.5:
                outcome = "loss"
            else:
                outcome = "be"
            re_db.close_signal(
                sig_id, exit_fill, outcome, round(leg_pts, 2),
                net_pnl_dollars=total_net,
                pnl_dollars=round(partial_booked + gross_leg, 2),
                balance_delta=round(net_leg, 2),
            )
            from backend.src.services.reversal_engine import ml_engine as re_ml
            re_ml.record_outcome(sig_id, outcome)
            try:
                from backend.src.db import database as _cdb_bus_sl
                _cdb_bus_sl.close_bus_entry("reversal_engine", sig_id)
            except Exception:
                pass
            try:
                from backend.src.services.cluster.sync.ledger import push_trade_closed
                push_trade_closed({
                    "trade_id":    sig.get("signal_ref") or str(sig_id),
                    "engine":      "reversal_engine",
                    "direction":   direction,
                    "strategy":    sig.get("strategy", ""),
                    "open_time":   sig.get("created_at"),
                    "close_time":  time.time(),
                    "pnl_dollars": total_net,
                    "outcome":     outcome,
                    "tg_source":   "Reversal Engine",
                    "mt5_ticket":  sig.get("mt5_ticket"),
                })
            except Exception as _le:
                _log.debug("[Ledger] push failed: %s", _le)
            _log.info(
                "[RE-Engine] CLOSED %s SL outcome=%s total_net=$%.2f (partials $%.2f + leg $%.2f)",
                sig.get("signal_ref", sig_id), outcome, total_net, partial_booked, net_leg
            )
            self._notify_refresh()

        elif hit_tp1:
            exit_fill = self._realistic_fill(tick, direction, closing=True)
            leg_pts   = _leg_pts(exit_fill)
            _gross, _net = self._net_pnl(sig, leg_pts, tick)
            leg_net   = round(_net * _TP1_FRAC, 2)
            re_db.book_partial_close(sig_id, leg_net, _TP1_FRAC, tp_idx=1)
            be_px = round(
                entry_ref + _cost_pts if direction == "BUY" else entry_ref - _cost_pts, 2
            )
            re_db.move_sl_to_be(sig_id, be_price=be_px)
            _log.info(
                "[RE-Engine] TP1 hit %s -> banked $%.2f (%.0f%%), SL -> BE+cost %.2f",
                sig.get("signal_ref", sig_id), leg_net, _TP1_FRAC * 100, be_px
            )
            self._notify_refresh()

        elif hit_mid:
            exit_fill = self._realistic_fill(tick, direction, closing=True)
            leg_pts   = _leg_pts(exit_fill)
            _gross, _net = self._net_pnl(sig, leg_pts, tick)
            leg_net   = round(_net * _MID_FRAC, 2)
            re_db.book_partial_close(sig_id, leg_net, _MID_FRAC, tp_idx=tp_mid_idx)
            re_db.set_stop_loss(sig_id, float(tp1))   # trail to TP1
            _log.info(
                "[RE-Engine] TP%d hit %s -> banked $%.2f (%.0f%%), SL -> TP1 %.2f",
                tp_mid_idx, sig.get("signal_ref", sig_id), leg_net, _MID_FRAC * 100, float(tp1)
            )
            self._notify_refresh()

        elif hit_final:
            exit_fill = self._realistic_fill(tick, direction, closing=True)
            leg_pts   = _leg_pts(exit_fill)
            gross_leg, net_leg = self._net_pnl(sig, leg_pts, tick)
            gross_leg *= remaining
            net_leg   *= remaining
            total_net = round(partial_booked + net_leg, 2)
            re_db.close_signal(
                sig_id, exit_fill, "win", round(leg_pts, 2),
                net_pnl_dollars=total_net,
                pnl_dollars=round(partial_booked + gross_leg, 2),
                balance_delta=round(net_leg, 2),
            )
            from backend.src.services.reversal_engine import ml_engine as re_ml
            re_ml.record_outcome(sig_id, "win")
            try:
                from backend.src.db import database as _cdb_bus_tp
                _cdb_bus_tp.close_bus_entry("reversal_engine", sig_id)
            except Exception:
                pass
            try:
                from backend.src.services.cluster.sync.ledger import push_trade_closed
                push_trade_closed({
                    "trade_id":    sig.get("signal_ref") or str(sig_id),
                    "engine":      "reversal_engine",
                    "direction":   direction,
                    "strategy":    sig.get("strategy", ""),
                    "open_time":   sig.get("created_at"),
                    "close_time":  time.time(),
                    "pnl_dollars": total_net,
                    "outcome":     "win",
                    "tg_source":   "Reversal Engine",
                    "mt5_ticket":  sig.get("mt5_ticket"),
                })
            except Exception as _le:
                _log.debug("[Ledger] push failed: %s", _le)
            _log.info(
                "[RE-Engine] TP7 hit %s total_net=$%.2f (partials $%.2f + runner $%.2f)",
                sig.get("signal_ref", sig_id), total_net, partial_booked, net_leg
            )
            self._notify_refresh()

    async def _manage_conservative_signal(self, sig: dict, tick) -> None:
        """Mirrors STRATEGY_CONSERVATIVE's real management (see
        core/models.py's STRATEGY_DESCRIPTIONS): the signal's own SL/TP
        levels are discarded entirely in favour of fixed distances from the
        actual fill price -- 5pt SL, 3pt TP1. TP1 books 80% and moves SL to
        breakeven (the fill price itself, no cost padding, per the
        documented behaviour); the remaining 20% then trails by 3pts,
        floored at breakeven, never loosening."""
        sig_id     = sig["id"]
        direction  = sig["direction"]
        entry_lo   = sig["entry_low"]
        entry_hi   = sig["entry_high"]
        entry_mid  = (entry_lo + entry_hi) / 2
        entry_ref  = float(sig.get("trigger_price") or entry_mid)
        remaining  = float(sig.get("remaining_frac") if sig.get("remaining_frac") is not None else 1.0)
        partial_booked = float(sig.get("partial_pnl_dollars") or 0.0)
        price      = float(tick.mid or tick.bid or 0)
        sign       = 1.0 if direction == "BUY" else -1.0

        fixed_sl  = entry_ref - sign * _CONSERVATIVE_SL_PTS
        fixed_tp1 = entry_ref + sign * _CONSERVATIVE_TP1_PTS

        def _leg_pts(exit_px: float) -> float:
            return (exit_px - entry_ref) if direction == "BUY" else (entry_ref - exit_px)

        def _close_remaining(outcome: str, stop_px: Optional[float] = None) -> None:
            # Both exits here are stop-type -- the fixed stop and the trail --
            # so both fill at their stop level rather than at the polled tick,
            # for the reason in _stop_fill. stop_px=None keeps the old
            # behaviour for any caller that is not a stop-out.
            exit_fill = (self._stop_fill(tick, direction, stop_px)
                         if stop_px is not None
                         else self._realistic_fill(tick, direction, closing=True))
            leg_pts   = _leg_pts(exit_fill)
            gross_leg, net_leg = self._net_pnl(sig, leg_pts, tick)
            gross_leg *= remaining
            net_leg   *= remaining
            total_net = round(partial_booked + net_leg, 2)
            final_outcome = outcome
            if outcome != "win":
                # Total realized result, same "banked partials count" rule
                # as the REF ladder's SL branch -- a trade that already
                # banked its 80% TP1 partial before the trail stops out is
                # still a net win, matching how a real account would read.
                if total_net > 0.5:
                    final_outcome = "win"
                elif total_net < -0.5:
                    final_outcome = "loss"
                else:
                    final_outcome = "be"
            re_db.close_signal(
                sig_id, exit_fill, final_outcome, round(leg_pts, 2),
                net_pnl_dollars=total_net,
                pnl_dollars=round(partial_booked + gross_leg, 2),
                balance_delta=round(net_leg, 2),
            )
            from backend.src.services.reversal_engine import ml_engine as re_ml
            re_ml.record_outcome(sig_id, final_outcome)
            try:
                from backend.src.db import database as _cdb_bus_cons
                _cdb_bus_cons.close_bus_entry("reversal_engine", sig_id)
            except Exception:
                pass
            try:
                from backend.src.services.cluster.sync.ledger import push_trade_closed
                push_trade_closed({
                    "trade_id":    sig.get("signal_ref") or str(sig_id),
                    "engine":      "reversal_engine",
                    "direction":   direction,
                    "strategy":    sig.get("strategy", ""),
                    "open_time":   sig.get("created_at"),
                    "close_time":  time.time(),
                    "pnl_dollars": total_net,
                    "outcome":     final_outcome,
                    "tg_source":   "Reversal Engine",
                    "mt5_ticket":  sig.get("mt5_ticket"),
                })
            except Exception as _le:
                _log.debug("[Ledger] push failed: %s", _le)
            _log.info(
                "[RE-Engine] CLOSED %s (conservative) outcome=%s total_net=$%.2f "
                "(partials $%.2f + leg $%.2f)",
                sig.get("signal_ref", sig_id), final_outcome, total_net, partial_booked, net_leg
            )
            self._notify_refresh()

        if remaining >= 1.0 - 1e-6:
            hit_sl  = price <= fixed_sl if direction == "BUY" else price >= fixed_sl
            hit_tp1 = price >= fixed_tp1 if direction == "BUY" else price <= fixed_tp1
            if hit_sl:
                _close_remaining("loss", fixed_sl)
            elif hit_tp1:
                exit_fill = self._realistic_fill(tick, direction, closing=True)
                leg_pts   = _leg_pts(exit_fill)
                _gross, _net = self._net_pnl(sig, leg_pts, tick)
                leg_net   = round(_net * _CONSERVATIVE_TP1_FRAC, 2)
                re_db.book_partial_close(sig_id, leg_net, _CONSERVATIVE_TP1_FRAC, tp_idx=1)
                re_db.move_sl_to_be(sig_id, be_price=round(entry_ref, 2))
                _log.info(
                    "[RE-Engine] TP1 hit %s (conservative) -> banked $%.2f (80%%), SL -> BE %.2f",
                    sig.get("signal_ref", sig_id), leg_net, entry_ref
                )
                self._notify_refresh()
            return

        # Runner phase: trail 3pts behind price, never loosening, floored at
        # the breakeven level move_sl_to_be already set above.
        current_sl = float(sig["stop_loss"])
        candidate_sl = price - sign * _CONSERVATIVE_TRAIL_PTS
        new_sl = max(current_sl, candidate_sl) if direction == "BUY" else min(current_sl, candidate_sl)
        if new_sl != current_sl:
            re_db.set_stop_loss(sig_id, new_sl)
            current_sl = new_sl

        hit_trail = price <= current_sl if direction == "BUY" else price >= current_sl
        if hit_trail:
            _close_remaining("win", current_sl)

    async def _template_leg_tickets(self, sig: dict, ticket: int) -> set:
        """Every broker position belonging to this signal's trade, not just
        the one ticket the signal recorded.

        Returns {ticket} unchanged for a normal single-position trade, and for
        anything this cannot resolve -- so a template lookup that fails
        degrades to exactly the previous behaviour rather than dropping the
        signal's own ticket.

        Only EA Template trades have siblings: the EA opens one position per
        Anchor/Grid leg while Python keeps a single trade row, and the legs
        are linked solely by the order comment the EA stamps on them.
        """
        legs = {int(ticket)}
        vsig = sig.get("vantage_signal_id")
        if not vsig:
            return legs
        try:
            from backend.src.db import database as _cdb
            from backend.src.services.broker.ea_bridge import (
                comment_for_trade, trade_id_prefix_from_comment,
            )

            def _trade_id():
                with _cdb.db() as conn:
                    row = conn.execute(
                        "SELECT trade_id, strategy FROM vantage_simulated_trades "
                        "WHERE signal_id=?", (vsig,),
                    ).fetchone()
                    return (row[0], row[1]) if row else (None, None)

            trade_id, strategy = await _cdb.to_db_thread(_trade_id)
            if not trade_id or not (strategy or "").startswith("template:"):
                return legs

            prefix = trade_id_prefix_from_comment(comment_for_trade(trade_id))
            if not prefix:
                return legs
            for d in (await self._bridge.get_deal_history(7) or []):
                if d.get("entry") != 0:
                    continue          # opening deals carry the EA's comment
                if trade_id_prefix_from_comment(d.get("comment") or "") == prefix:
                    pid = d.get("position_id")
                    if pid:
                        legs.add(int(pid))
        except Exception as exc:
            _log.debug("[RE-Engine] leg lookup failed for ticket=%s: %s", ticket, exc)
            return {int(ticket)}
        if len(legs) > 1:
            _log.info("[RE-Engine] signal %s spans %d template legs: %s",
                      sig.get("signal_ref", sig.get("id")), len(legs), sorted(legs))
        return legs

    async def _reconcile_live_signal(self, sig: dict) -> None:
        """Mirror a live-executed signal's outcome/P&L from the real MT5
        trade rather than the tick-based simulation used for virtual
        signals -- see the call site in _check_outcomes for why this exists.

        A ticket missing from get_positions() closes the position, but a
        single miss can be a transient bridge hiccup (same debounce pattern
        as core.engine._sync_closed_mt5_positions), so this requires
        _LIVE_MISSING_THRESHOLD consecutive misses before treating the
        signal as actually closed.
        """
        sig_id = sig["id"]
        ticket = int(sig["mt5_ticket"])
        try:
            live_positions = await self._bridge.get_positions()
        except Exception as exc:
            _log.debug("[RE-Engine] live reconcile: get_positions failed ticket=%s: %s", ticket, exc)
            return
        # EA Template legs: the signal's mt5_ticket is only the ONE leg that
        # promoted the trade row. A grid trade opens an anchor plus N pending
        # legs as separate broker positions, so reconciling that single ticket
        # counted roughly a quarter of the real result into the virtual
        # balance and closed the signal while sibling legs were still running.
        # The EA's order comment is the only link to them (see ea_bridge.
        # comment_for_trade), the same link the History channel lookup uses.
        leg_tickets = await self._template_leg_tickets(sig, ticket)

        if any(int(p.get("ticket", 0)) in leg_tickets for p in live_positions):
            self._live_missing_streak.pop(ticket, None)
            return  # still open on the real account -- nothing to reconcile yet

        streak = self._live_missing_streak.get(ticket, 0) + 1
        self._live_missing_streak[ticket] = streak
        if streak < _LIVE_MISSING_THRESHOLD:
            return

        deals: list = []
        for _t in sorted(leg_tickets):
            try:
                deals.extend(await self._bridge.get_position_history(_t) or [])
            except Exception as exc:
                _log.debug("[RE-Engine] live reconcile: get_position_history failed "
                           "ticket=%s: %s", _t, exc)
                if _t == ticket:
                    return
        if not deals:
            return  # nothing to reconcile yet -- try again next cycle

        direction  = sig["direction"]
        open_type  = 0 if direction == "BUY" else 1
        close_deals = [d for d in deals if d.get("entry") in (1, 2, 3)]
        if not close_deals:
            close_deals = [d for d in deals if d.get("type") != open_type]
        if not close_deals:
            return

        close_price = max(close_deals, key=lambda d: d.get("time", 0)).get("price")
        net_pnl = round(sum(
            float(d.get("profit", 0)) + float(d.get("swap", 0)) + float(d.get("fee", 0))
            for d in close_deals
        ), 2)
        entry_ref = float(sig.get("trigger_price") or ((sig["entry_low"] + sig["entry_high"]) / 2))
        pnl_pts = round(
            (float(close_price) - entry_ref) if direction == "BUY" else (entry_ref - float(close_price)), 2
        ) if close_price else 0.0

        if net_pnl > 0.5:
            outcome = "win"
        elif net_pnl < -0.5:
            outcome = "loss"
        else:
            outcome = "be"

        re_db.close_signal(
            sig_id, float(close_price or entry_ref), outcome, pnl_pts,
            net_pnl_dollars=net_pnl, pnl_dollars=net_pnl, balance_delta=net_pnl,
        )
        from backend.src.services.reversal_engine import ml_engine as re_ml
        re_ml.record_outcome(sig_id, outcome)
        self._live_missing_streak.pop(ticket, None)
        try:
            from backend.src.db import database as _cdb_bus_live
            _cdb_bus_live.close_bus_entry("reversal_engine", sig_id)
        except Exception:
            pass
        try:
            from backend.src.services.cluster.sync.ledger import push_trade_closed
            push_trade_closed({
                "trade_id":    sig.get("signal_ref") or str(sig_id),
                "engine":      "reversal_engine",
                "direction":   direction,
                "strategy":    sig.get("strategy", ""),
                "open_time":   sig.get("created_at"),
                "close_time":  time.time(),
                "pnl_dollars": net_pnl,
                "outcome":     outcome,
                "tg_source":   "Reversal Engine",
                "mt5_ticket":  ticket,
            })
        except Exception as _le:
            _log.debug("[Ledger] push failed: %s", _le)
        _log.info(
            "[RE-Engine] live reconciled %s ticket=%s outcome=%s real_net=$%.2f",
            sig.get("signal_ref", sig_id), ticket, outcome, net_pnl,
        )
        self._notify_refresh()
