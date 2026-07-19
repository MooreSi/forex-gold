"""TP/SL/partial-close management + live-P&L reconciliation for the
Breakout Engine -- extracted verbatim (no logic changes) from engine.py's
_close_and_learn / _reconcile_live_pnl / the triggered-signal branch of
_check_outcomes, as part of task 030. See
docs/todo/refactor/breakout-signal-migration/030-*.md.

_ManagementMixin is composed into BreakoutEngine (breakout_signal_service.py).

Two raw-SQL bypasses of database.py's own API (both touching bo_signals,
found while extracting _reconcile_live_pnl) now go through the new repo
functions `get_closed_or_expired_signals_with_mt5_ticket()` and
`promote_expired_to_closed()` instead -- same behavior, proper API. The
cross-engine read into the CORE database's `vantage_simulated_trades` table
is left as-is (same precedent as gd_copy_signal_correlate.py's VIP read --
inherent cross-engine coupling, not a bypass of this module's own repo).
"""
from __future__ import annotations

import logging
import time

from forex_trader.breakout_signal import breakout_signal_repo as bdb
from forex_trader.breakout_signal import adaptive_params as ap

_log = logging.getLogger("breakout_signal")

_TP1_FRAC = 0.33
_TP2_FRAC = 0.33


class _ManagementMixin:
    def _compute_cost_pts(self, spread_raw: float) -> float:
        """Round-trip trade cost in price units (same scale as pnl_pts)."""
        try:
            from forex_trader.core import database as cdb
            fs = cdb.get_fee_settings()
            slippage_pts = fs.get("estimated_slippage_points", 5.0) * 0.01
            commission_rt = (
                fs.get("commission_per_lot_per_side", 0.0) * 2
                + fs.get("commission_round_turn_per_lot", 0.0)
            )
            commission_pts = commission_rt / 100.0
            return round(spread_raw + slippage_pts + commission_pts, 4)
        except Exception:
            return 0.35  # conservative fallback (0.30 spread + 0.05 slippage)

    def _close_and_learn(
        self,
        sig_id: int,
        close_price: float,
        outcome: str,
        note: str,
        entry: float,
        direction: str,
        lot: float,
        cost_pts: float,
    ) -> None:
        """Close a signal, compute net P&L, record ML outcome, trigger batch
        analysis. NOTE: preserves the known close_signal balance
        double-counting bug (see breakout_signal_repo.py's module
        docstring) -- not fixed here, this is a structural extraction."""
        sig             = bdb.get_signal_by_id(sig_id)
        remaining_frac  = float(sig.get("remaining_frac") if sig and sig.get("remaining_frac") is not None else 1.0)
        partial_booked  = float(sig.get("partial_pnl_dollars") or 0.0) if sig else 0.0

        raw_pts  = (close_price - entry) if direction == "BUY" else (entry - close_price)
        pnl_pts  = round(raw_pts, 2)
        leg_net_pts  = round(pnl_pts - cost_pts, 4)
        leg_net_dol  = round(leg_net_pts * lot * remaining_frac * 100.0, 2)
        net_dol      = round(partial_booked + leg_net_dol, 2)
        net_pts      = round(net_dol / (lot * 100.0), 4) if lot else 0.0

        ml_outcome = outcome
        if net_dol > 0.5:
            ml_outcome = "win"
        elif net_dol < -0.5:
            ml_outcome = "loss"
        else:
            ml_outcome = "be"

        bdb.close_signal(
            sig_id, close_price, ml_outcome, note,
            net_pnl_pts=net_pts, net_pnl_dollars=net_dol,
        )

        try:
            from forex_trader.sync.ledger import push_trade_closed
            push_trade_closed({
                "trade_id":    sig.get("signal_ref") or str(sig_id),
                "engine":      "breakout",
                "direction":   direction,
                "strategy":    sig.get("strategy", "") if sig else "",
                "open_time":   sig.get("created_at") if sig else None,
                "close_time":  time.time(),
                "pnl_dollars": net_dol,
                "outcome":     ml_outcome,
                "tg_source":   "Breakout Engine",
                "mt5_ticket":  sig.get("mt5_ticket") if sig else None,
            })
        except Exception as _le:
            _log.debug("[Ledger] push failed: %s", _le)

        mt5_ticket = sig.get("mt5_ticket") if sig else None
        if mt5_ticket:
            try:
                from forex_trader.core import database as _mdb
                with _mdb.db() as _mc:
                    _mr = _mc.execute(
                        "SELECT mt5_profit FROM vantage_simulated_trades"
                        " WHERE mt5_ticket=? AND status='closed'",
                        (mt5_ticket,),
                    ).fetchone()
                if _mr and _mr[0] is not None:
                    actual_profit = float(_mr[0])
                    ml_outcome = (
                        "win"  if actual_profit > 1.0  else
                        "loss" if actual_profit < -1.0 else
                        "be"
                    )
                    bdb.update_signal_pnl_from_mt5(sig_id, actual_profit, ml_outcome)
                    _log.debug(
                        "[BO-Engine] signal %d resolved to MT5 profit $%.2f (%s)",
                        sig_id, actual_profit, ml_outcome,
                    )
            except Exception as _e:
                _log.debug("[BO-Engine] MT5 P&L lookup failed for sig %d: %s", sig_id, _e)

        from forex_trader.breakout_signal import ml_engine as bo_ml
        bo_ml.record_outcome(sig_id, ml_outcome)

        try:
            from forex_trader.core import database as _cdb_bus
            _cdb_bus.close_bus_entry("breakout", sig_id)
        except Exception:
            pass

        self._closed_count += 1
        if self._closed_count % 10 == 0:  # _BATCH_REVIEW_EVERY, mirrored from breakout_signal_learn.py
            import asyncio
            asyncio.ensure_future(self._run_batch_analysis())

    def _manage_triggered_signal(
        self, sig: dict, bid: float, ask: float, now: float, cost_pts: float,
    ) -> None:
        """The time-stop / SL / TP3 / TP2-partial / TP1-partial ladder for a
        triggered, non-live-managed signal -- extracted from _check_outcomes.
        Caller (_check_outcomes in breakout_signal_service.py) has already
        handled: live-execution closure sync, orphan watchdog, and
        pending->triggered auto-trigger."""
        sig_id    = sig["id"]
        direction = sig["direction"]
        entry     = float(sig.get("entry_mid", 0) or 0)
        lot       = float(sig.get("lot_size") or 0.10)
        sl        = float(sig.get("stop_loss", 0) or 0)
        tp1       = float(sig.get("tp1", 0) or 0)
        tp2       = float(sig.get("tp2", 0) or 0)
        tp3       = float(sig.get("tp3", 0) or 0)
        trigger_time = float(sig.get("trigger_time") or 0)
        sl_moved  = bool(sig.get("sl_moved_to_be", 0))

        eval_price = bid if direction == "BUY" else ask

        # ── Time stop ─────────────────────────────────────────────────────
        _tp_trig = float(sig.get("trigger_price") or entry or 0)
        if (_tp_trig > 0 and trigger_time > 0
                and float(sig.get("remaining_frac") or 1.0) >= 0.999):
            _unreal_bo = round(
                (eval_price - _tp_trig) if direction == "BUY"
                else (_tp_trig - eval_price), 2,
            )
            _sl_dist_bo = float(sig.get("sl_dist") or 0) or abs(entry - sl) or 1.0
            _ts_mins_bo = ap.get("time_stop_mins")
            if ((now - trigger_time) > _ts_mins_bo * 60
                    and _unreal_bo < -0.2 * _sl_dist_bo):
                _ts_outcome = "loss" if _unreal_bo < -1.0 else "be"
                self._close_and_learn(
                    sig_id, eval_price, _ts_outcome,
                    f"Time stop after {int((now - trigger_time) / 60)}min @ {_unreal_bo:+.1f}pts",
                    entry, direction, lot, cost_pts,
                )
                _log.info(
                    "[BO-Engine] %s time stop → %s (%+.1fpts after %dmin)",
                    sig.get("signal_ref"), _ts_outcome.upper(), _unreal_bo,
                    int((now - trigger_time) / 60),
                )
                self._notify_refresh()
                return

        # SL hit
        sl_hit = (direction == "BUY" and eval_price <= sl) or \
                 (direction == "SELL" and eval_price >= sl)
        if sl_hit:
            outcome = "be" if sl_moved else "loss"
            self._close_and_learn(
                sig_id, eval_price, outcome, f"SL @ {eval_price:.2f}",
                entry, direction, lot, cost_pts,
            )
            _log.info("[BO-Engine] %s %s", sig.get("signal_ref"), outcome.upper())
            self._notify_refresh()
            return

        # TP3 — full winner, close position
        tp3_hit = tp3 and (
            (direction == "BUY"  and eval_price >= tp3) or
            (direction == "SELL" and eval_price <= tp3)
        )
        if tp3_hit:
            self._close_and_learn(
                sig_id, eval_price, "win", f"TP3 @ {eval_price:.2f}",
                entry, direction, lot, cost_pts,
            )
            _log.info("[BO-Engine] %s WIN (TP3)", sig.get("signal_ref"))
            self._notify_refresh()
            return

        remaining_frac = float(
            sig.get("remaining_frac") if sig.get("remaining_frac") is not None else 1.0
        )

        # TP2 — bank a second partial, trail SL to TP1
        tp2_hit = tp2 and (1.0 - _TP1_FRAC - _TP2_FRAC) < remaining_frac <= (1.0 - _TP1_FRAC) + 1e-6 and (
            (direction == "BUY"  and eval_price >= tp2) or
            (direction == "SELL" and eval_price <= tp2)
        )
        if tp2_hit:
            leg_pts     = (eval_price - entry) if direction == "BUY" else (entry - eval_price)
            leg_net_dol = round((leg_pts - cost_pts) * lot * _TP2_FRAC * 100.0, 2)
            bdb.book_partial_close(sig_id, leg_net_dol, _TP2_FRAC, f"TP2 partial @ {eval_price:.2f}")
            bdb.set_stop_loss(sig_id, tp1)
            _log.info(
                "[BO-Engine] %s TP2 partial booked $%.2f — SL → TP1 %.2f",
                sig.get("signal_ref"), leg_net_dol, tp1,
            )
            self._notify_refresh()
            return

        # TP1 — bank first partial, move SL to break-even PLUS the round-trip cost
        tp1_hit = tp1 and remaining_frac >= 1.0 - 1e-6 and (
            (direction == "BUY"  and eval_price >= tp1) or
            (direction == "SELL" and eval_price <= tp1)
        )
        if tp1_hit:
            leg_pts     = (eval_price - entry) if direction == "BUY" else (entry - eval_price)
            leg_net_dol = round((leg_pts - cost_pts) * lot * _TP1_FRAC * 100.0, 2)
            bdb.book_partial_close(sig_id, leg_net_dol, _TP1_FRAC, f"TP1 partial @ {eval_price:.2f}")
            be_px = round(entry + cost_pts, 2) if direction == "BUY" else round(entry - cost_pts, 2)
            bdb.move_sl_to_be(sig_id, be_price=be_px)
            _log.info(
                "[BO-Engine] %s TP1 partial booked $%.2f — SL → BE+cost %.2f",
                sig.get("signal_ref"), leg_net_dol, be_px,
            )
            self._notify_refresh()

    async def _reconcile_live_pnl(self) -> None:
        """
        At startup, sync P&L for any closed bo_signal that has an mt5_ticket
        but is showing the virtual simulation figure instead of the actual
        MT5 profit.
        """
        try:
            from forex_trader.core import database as _mdb

            def _do_reconcile() -> int:
                import sqlite3 as _sqlite3
                rows = bdb.get_closed_or_expired_signals_with_mt5_ticket()
                if not rows:
                    return 0
                _db_path = _mdb._DB_PATH  # type: ignore[attr-defined]
                main_conn = _sqlite3.connect(f"file:{_db_path}?mode=ro", uri=True)
                main_conn.row_factory = _sqlite3.Row
                updated = 0
                try:
                    for row in rows:
                        sig_id, ticket, sim_pnl, sim_outcome, sig_status = (
                            row["id"], row["mt5_ticket"], row["pnl_dollars"] or 0.0,
                            row["outcome"] or "", row["status"] or "",
                        )
                        cur = main_conn.execute(
                            "SELECT mt5_profit, status FROM vantage_simulated_trades"
                            " WHERE mt5_ticket=? AND status='closed'",
                            (ticket,),
                        )
                        main_row = cur.fetchone()
                        if not main_row:
                            continue
                        actual_profit = float(main_row["mt5_profit"] or 0.0)
                        if abs(actual_profit - float(sim_pnl)) < 0.01 and sig_status == "closed":
                            continue
                        mt5_outcome = (
                            "win"  if actual_profit > 1.0  else
                            "loss" if actual_profit < -1.0 else
                            "be"
                        )
                        bdb.update_signal_pnl_from_mt5(sig_id, actual_profit, mt5_outcome)
                        if sig_status == "expired":
                            bdb.promote_expired_to_closed(sig_id)
                            _log.info(
                                "[BO-Engine] Expired live signal %d (ticket %s) promoted to closed: MT5=$%.2f (%s)",
                                sig_id, ticket, actual_profit, mt5_outcome,
                            )
                        if sim_outcome != mt5_outcome:
                            from forex_trader.breakout_signal import ml_engine as bo_ml
                            bo_ml.record_outcome(sig_id, mt5_outcome)
                        updated += 1
                        _log.info(
                            "[BO-Engine] Reconciled signal %d (ticket %s): sim=$%.2f → MT5=$%.2f (%s)",
                            sig_id, ticket, sim_pnl, actual_profit, mt5_outcome,
                        )
                finally:
                    main_conn.close()
                return updated

            updated = await _mdb.to_db_thread(_do_reconcile)
            if updated:
                _log.info("[BO-Engine] Reconciled %d live P&L records with MT5 actuals", updated)
        except Exception as exc:
            _log.warning("[BO-Engine] P&L reconciliation failed: %s", exc)
