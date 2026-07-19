"""TP/SL/partial-close management for triggered GD Copy signals -- extracted
verbatim (no logic changes) from engine.py's _check_outcomes as part of
task 040. See docs/todo/refactor/backend-foundation/040-*.md.

_ManagementMixin is composed into GDCopyEngine (gd_copy_signal_service.py)
-- these methods rely on `self` attributes (_main_eng, _live_missing_streak,
_notify_refresh) defined there, exactly as they did when this code lived
inline in engine.py.
"""
from __future__ import annotations

import logging
import time

from forex_trader.gd_copy_signal import gd_copy_signal_repo as gdc_db

_log = logging.getLogger("gd_copy_signal")

_LIVE_MISSING_THRESHOLD = 3   # consecutive get_positions() misses before treating a live ticket as closed

# Lot size for virtual P&L tracking (not live yet)
_VIRTUAL_LOT = 0.1


class _ManagementMixin:
    def _realistic_fill(self, tick, direction: str, closing: bool) -> float:
        """Approximate a real MT5 fill price: buys fill at ask, sells at bid.
        closing=True means we're exiting the position (opposite side of entry)."""
        buy_side = (direction == "BUY") != closing
        return float(tick.ask if buy_side else tick.bid) or float(tick.mid or 0)

    def _net_pnl(self, sig: dict, pnl_pts: float, exit_tick) -> tuple[float, float]:
        """(gross_pnl_dollars, net_pnl_dollars) after spread/commission/slippage,
        using the same fee model as real GD VIP trades on the main engine."""
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
        """VIP-style ladder scale-out for a signal that is triggered and NOT
        live-managed (see _check_outcomes: live-executed signals are routed
        to _reconcile_live_signal instead, never here).

        The old logic moved SL to raw entry at TP1 and only counted a "win"
        at TP7 -- 88 of 118 TP1-hitting signals closed 'be' with $0 banked,
        tanking both the reported win rate and the ML labels. GD VIP itself
        calls TP1 a win; we bank 33% there, 33% at the mid-ladder TP, and
        let 34% ride to TP7."""
        sig_id     = sig["id"]
        direction  = sig["direction"]
        sl         = sig["stop_loss"]
        tp1        = sig["tp1"]
        # GD2/Unicorn signals only ever carry tp1/tp2/tp3 (see
        # signal_generator.calculate_gd2_tp_structure -- 1R partial, 2R
        # partial+BE, 6R runner), not the VIP 8-level ladder, so they
        # need their own mapping rather than falling through the VIP
        # tp4/tp7 lookups (which would otherwise resolve to None and
        # leave a GD2 signal that can never reach its "final" branch).
        if sig.get("strategy") == "gd2_unicorn":
            tp_mid = sig.get("tp2") or tp1
            tp7    = sig.get("tp3") or tp1
        else:
            tp_mid = sig.get("tp4") or sig.get("tp3") or tp1   # mid-ladder partial
            tp7    = sig.get("tp7") or sig.get("tp8") or tp1
        entry_lo  = sig["entry_low"]
        entry_hi  = sig["entry_high"]
        entry_mid = (entry_lo + entry_hi) / 2
        entry_ref = float(sig.get("trigger_price") or entry_mid)  # realistic fill once triggered
        remaining = float(sig.get("remaining_frac") if sig.get("remaining_frac") is not None else 1.0)
        partial_booked = float(sig.get("partial_pnl_dollars") or 0.0)
        price = float(tick.mid or tick.bid or 0)

        if direction == "BUY":
            hit_sl    = price <= sl
            hit_tp1   = price >= tp1 and remaining >= 1.0 - 1e-6
            hit_mid   = (tp_mid and price >= float(tp_mid)
                         and 0.34 < remaining <= 0.67 + 1e-6)
            hit_final = tp7 and price >= float(tp7)
        else:
            hit_sl    = price >= sl
            hit_tp1   = price <= tp1 and remaining >= 1.0 - 1e-6
            hit_mid   = (tp_mid and price <= float(tp_mid)
                         and 0.34 < remaining <= 0.67 + 1e-6)
            hit_final = tp7 and price <= float(tp7)

        # Round-trip cost in points for BE placement (spread + slippage).
        _cost_pts = round((float(tick.ask) - float(tick.bid)) + 0.05, 2)

        def _leg_pts(exit_px: float) -> float:
            return (exit_px - entry_ref) if direction == "BUY" else (entry_ref - exit_px)

        if hit_sl:
            exit_fill = self._realistic_fill(tick, direction, closing=True)
            leg_pts   = _leg_pts(exit_fill)
            gross_leg, net_leg = self._net_pnl(sig, leg_pts, tick)
            gross_leg *= remaining
            net_leg   *= remaining
            total_net = round(partial_booked + net_leg, 2)
            # Outcome by TOTAL realized result -- a trade that banked TP1
            # partials before the BE stop is a win, matching VIP accounting.
            if total_net > 0.5:
                outcome = "win"
            elif total_net < -0.5:
                outcome = "loss"
            else:
                outcome = "be"
            gdc_db.close_signal(
                sig_id, exit_fill, outcome, round(leg_pts, 2),
                net_pnl_dollars=total_net,
                pnl_dollars=round(partial_booked + gross_leg, 2),
                balance_delta=round(net_leg, 2),
            )
            from forex_trader.gd_copy_signal import ml_engine as gdc_ml
            gdc_ml.record_outcome(sig_id, outcome)
            try:
                from forex_trader.core import database as _cdb_bus_sl
                _cdb_bus_sl.close_bus_entry("gd_copy", sig_id)
            except Exception:
                pass
            try:
                from forex_trader.sync.ledger import push_trade_closed
                push_trade_closed({
                    "trade_id":    sig.get("signal_ref") or str(sig_id),
                    "engine":      "gd_copy",
                    "direction":   direction,
                    "strategy":    sig.get("strategy", ""),
                    "open_time":   sig.get("created_at"),
                    "close_time":  time.time(),
                    "pnl_dollars": total_net,
                    "outcome":     outcome,
                    "tg_source":   "GD Copy Engine",
                    "mt5_ticket":  sig.get("mt5_ticket"),
                })
            except Exception as _le:
                _log.debug("[Ledger] push failed: %s", _le)
            _log.info(
                "[GDC-Engine] CLOSED %s SL outcome=%s total_net=$%.2f (partials $%.2f + leg $%.2f)",
                sig.get("signal_ref", sig_id), outcome, total_net, partial_booked, net_leg
            )
            self._notify_refresh()

        elif hit_tp1:
            exit_fill = self._realistic_fill(tick, direction, closing=True)
            leg_pts   = _leg_pts(exit_fill)
            _gross, _net = self._net_pnl(sig, leg_pts, tick)
            leg_net   = round(_net * 0.33, 2)
            gdc_db.book_partial_close(sig_id, leg_net, 0.33, tp_idx=1)
            be_px = round(
                entry_ref + _cost_pts if direction == "BUY" else entry_ref - _cost_pts, 2
            )
            gdc_db.move_sl_to_be(sig_id, be_price=be_px)
            _log.info(
                "[GDC-Engine] TP1 hit %s -> banked $%.2f (33%%), SL -> BE+cost %.2f",
                sig.get("signal_ref", sig_id), leg_net, be_px
            )
            self._notify_refresh()

        elif hit_mid:
            exit_fill = self._realistic_fill(tick, direction, closing=True)
            leg_pts   = _leg_pts(exit_fill)
            _gross, _net = self._net_pnl(sig, leg_pts, tick)
            leg_net   = round(_net * 0.33, 2)
            gdc_db.book_partial_close(sig_id, leg_net, 0.33, tp_idx=4)
            gdc_db.set_stop_loss(sig_id, float(tp1))   # trail to TP1
            _log.info(
                "[GDC-Engine] TP4 hit %s -> banked $%.2f (33%%), SL -> TP1 %.2f",
                sig.get("signal_ref", sig_id), leg_net, float(tp1)
            )
            self._notify_refresh()

        elif hit_final:
            exit_fill = self._realistic_fill(tick, direction, closing=True)
            leg_pts   = _leg_pts(exit_fill)
            gross_leg, net_leg = self._net_pnl(sig, leg_pts, tick)
            gross_leg *= remaining
            net_leg   *= remaining
            total_net = round(partial_booked + net_leg, 2)
            gdc_db.close_signal(
                sig_id, exit_fill, "win", round(leg_pts, 2),
                net_pnl_dollars=total_net,
                pnl_dollars=round(partial_booked + gross_leg, 2),
                balance_delta=round(net_leg, 2),
            )
            from forex_trader.gd_copy_signal import ml_engine as gdc_ml
            gdc_ml.record_outcome(sig_id, "win")
            try:
                from forex_trader.core import database as _cdb_bus_tp
                _cdb_bus_tp.close_bus_entry("gd_copy", sig_id)
            except Exception:
                pass
            try:
                from forex_trader.sync.ledger import push_trade_closed
                push_trade_closed({
                    "trade_id":    sig.get("signal_ref") or str(sig_id),
                    "engine":      "gd_copy",
                    "direction":   direction,
                    "strategy":    sig.get("strategy", ""),
                    "open_time":   sig.get("created_at"),
                    "close_time":  time.time(),
                    "pnl_dollars": total_net,
                    "outcome":     "win",
                    "tg_source":   "GD Copy Engine",
                    "mt5_ticket":  sig.get("mt5_ticket"),
                })
            except Exception as _le:
                _log.debug("[Ledger] push failed: %s", _le)
            _log.info(
                "[GDC-Engine] TP7 hit %s total_net=$%.2f (partials $%.2f + runner $%.2f)",
                sig.get("signal_ref", sig_id), total_net, partial_booked, net_leg
            )
            self._notify_refresh()

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
            _log.debug("[GDC-Engine] live reconcile: get_positions failed ticket=%s: %s", ticket, exc)
            return
        if any(int(p.get("ticket", 0)) == ticket for p in live_positions):
            self._live_missing_streak.pop(ticket, None)
            return  # still open on the real account -- nothing to reconcile yet

        streak = self._live_missing_streak.get(ticket, 0) + 1
        self._live_missing_streak[ticket] = streak
        if streak < _LIVE_MISSING_THRESHOLD:
            return

        try:
            deals = await self._bridge.get_position_history(ticket)
        except Exception as exc:
            _log.debug("[GDC-Engine] live reconcile: get_position_history failed ticket=%s: %s", ticket, exc)
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

        gdc_db.close_signal(
            sig_id, float(close_price or entry_ref), outcome, pnl_pts,
            net_pnl_dollars=net_pnl, pnl_dollars=net_pnl, balance_delta=net_pnl,
        )
        from forex_trader.gd_copy_signal import ml_engine as gdc_ml
        gdc_ml.record_outcome(sig_id, outcome)
        self._live_missing_streak.pop(ticket, None)
        try:
            from forex_trader.core import database as _cdb_bus_live
            _cdb_bus_live.close_bus_entry("gd_copy", sig_id)
        except Exception:
            pass
        try:
            from forex_trader.sync.ledger import push_trade_closed
            push_trade_closed({
                "trade_id":    sig.get("signal_ref") or str(sig_id),
                "engine":      "gd_copy",
                "direction":   direction,
                "strategy":    sig.get("strategy", ""),
                "open_time":   sig.get("created_at"),
                "close_time":  time.time(),
                "pnl_dollars": net_pnl,
                "outcome":     outcome,
                "tg_source":   "GD Copy Engine",
                "mt5_ticket":  ticket,
            })
        except Exception as _le:
            _log.debug("[Ledger] push failed: %s", _le)
        _log.info(
            "[GDC-Engine] live reconciled %s ticket=%s outcome=%s real_net=$%.2f",
            sig.get("signal_ref", sig_id), ticket, outcome, net_pnl,
        )
        self._notify_refresh()
