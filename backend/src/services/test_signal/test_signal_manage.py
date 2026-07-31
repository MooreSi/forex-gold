"""TP/SL/time-stop management + close + live-P&L reconciliation for
TestSignalEngine (Bounce) -- extracted verbatim (no logic changes) from
engine.py's _close_signal / _generate_learning_note / _reconcile_live_pnl /
the triggered-signal branch of _check_outcomes, as part of task 030. See
docs/todo/refactor/test-signal-migration/030-*.md.

_ManagementMixin is composed into TestSignalEngine (test_signal_service.py).

_close_signal now calls the new atomic repo function
close_signal_with_balance_update() instead of database.py's 4 separate
calls -- see test_signal_repo.py's module docstring and 020's task notes
for why learning_note must be computed BEFORE that call (async AI call
can't happen inside an open DB transaction). One raw-SQL bypass fixed
while extracting _reconcile_live_pnl (get_closed_signals_with_mt5_ticket,
added in 020); the cross-engine read into the CORE database's
vantage_simulated_trades table is left as-is, same precedent as the other
two engines' manage.py files.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import backend.src.config as cfg_module
from backend.src.services.ai import provider as ai_provider

from backend.src.services.test_signal import test_signal_repo as tdb
from backend.src.services.test_signal import adaptive_params as ap
from backend.src.services.test_signal import ml_engine as ml

_log = logging.getLogger("test_signal")

_MIN_LOT = 0.01
_CONSERVATIVE_STRATS = {"conservative", "conservative_trial"}
_BATCH_REVIEW_EVERY = 10


def _calc_pnl_dollars(pnl_pts: float, lot_size: float) -> float:
    return round(pnl_pts * lot_size * 100.0, 2)


def _compute_cost_pts(spread_raw: float = 0.30) -> float:
    """
    Estimated round-trip trade cost in price units (same scale as pnl_pts).
    Covers: spread + slippage + commission.
    `spread_raw` = tick.ask - tick.bid at the time of close.
    """
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
        return round(spread_raw + 0.05, 4)


class _ManagementMixin:
    async def _reconcile_live_pnl(self) -> None:
        """Sync closed test_signal P&L with actual MT5 profit where they diverge."""
        try:
            from forex_trader.core import database as _mdb

            def _do_reconcile() -> int:
                import sqlite3 as _sqlite3
                rows = tdb.get_closed_signals_with_mt5_ticket()
                if not rows:
                    return 0
                _db_path = _mdb._DB_PATH  # type: ignore[attr-defined]
                main_conn = _sqlite3.connect(f"file:{_db_path}?mode=ro", uri=True)
                main_conn.row_factory = _sqlite3.Row
                updated = 0
                try:
                    for row in rows:
                        sig_id, ticket, sim_pnl, sim_outcome = (
                            row["id"], row["mt5_ticket"], row["pnl_dollars"] or 0.0,
                            row["outcome"] or "",
                        )
                        cur = main_conn.execute(
                            "SELECT mt5_profit FROM vantage_simulated_trades"
                            " WHERE mt5_ticket=? AND status='closed'",
                            (ticket,),
                        )
                        main_row = cur.fetchone()
                        if not main_row:
                            continue
                        actual_profit = float(main_row["mt5_profit"] or 0.0)
                        if abs(actual_profit - float(sim_pnl)) < 0.01:
                            continue
                        mt5_outcome = (
                            "win"  if actual_profit > 1.0  else
                            "loss" if actual_profit < -1.0 else
                            "be"
                        )
                        tdb.update_signal_pnl_from_mt5(sig_id, actual_profit, mt5_outcome)
                        if sim_outcome != mt5_outcome:
                            ml.record_outcome(sig_id, mt5_outcome)
                        updated += 1
                        _log.info(
                            "[TestSignal] Reconciled SIG-%04d (ticket %s): sim=$%.2f → MT5=$%.2f (%s)",
                            sig_id, ticket, sim_pnl, actual_profit, mt5_outcome,
                        )
                finally:
                    main_conn.close()
                return updated

            updated = await _mdb.to_db_thread(_do_reconcile)
            if updated:
                _log.info("[TestSignal] Reconciled %d live P&L records with MT5 actuals", updated)
        except Exception as exc:
            _log.warning("[TestSignal] P&L reconciliation failed: %s", exc)

    async def _generate_learning_note(self, signal_id: int, outcome: str,
                                      pnl_pts: float, pnl_dollars: float) -> str:
        """Ask the configured AI provider for a one-line post-trade explanation."""
        cfg = cfg_module.load()
        if not ai_provider.is_configured(cfg):
            return ""

        try:
            sig = tdb.get_signal_by_id(signal_id)
            if not sig:
                return ""

            prompt = (
                f"Trade closed: {sig.get('direction')} XAUUSD\n"
                f"Outcome: {outcome}  PnL: {pnl_pts:+.2f} pts (${pnl_dollars:+.2f})\n"
                f"Entry: ${sig.get('entry_mid', 0):.2f}  SL: ${sig.get('stop_loss', 0):.2f}  "
                f"TP1: ${sig.get('tp1') or 0:.2f}  TP3: ${sig.get('tp3') or 0:.2f}\n"
                f"Session: {sig.get('session')}  HTF bias: {sig.get('htf_bias')}\n"
                f"Key level type: {sig.get('key_level_type')}  "
                f"Quality score: {sig.get('quality_score', 0):.0%}\n"
                f"Signal rationale: {sig.get('rationale', '')}\n\n"
                "In ONE sentence, explain the most likely reason this trade won or lost "
                "and the single most important improvement for future signals of this type."
            )
            return await ai_provider.complete(cfg, "", prompt, max_tokens=120, timeout=15)
        except Exception as e:
            _log.debug("[TestSignal] Learning note error: %s", e)
            return ""

    async def _close_signal(self, signal_id: int, outcome: str, close_px: float,
                            pnl_pts: float, lot_size: float, direction: str,
                            spread: float = 0.30) -> None:
        # Clear the signal bus entry immediately so other engines see it as resolved
        try:
            from forex_trader.core import database as _cdb_bus_close
            _cdb_bus_close.close_bus_entry("bounce", signal_id)
        except Exception:
            pass

        # Gross P&L (price-level based, no costs)
        pnl_dollars = _calc_pnl_dollars(pnl_pts, lot_size)

        # Cost-adjusted P&L — what a real trader actually nets after spread/commission/slippage
        cost_pts      = _compute_cost_pts(spread)
        net_pnl_pts   = round(pnl_pts - cost_pts, 4)
        net_pnl_dol   = _calc_pnl_dollars(net_pnl_pts, lot_size)

        ref = f"SIG-{signal_id:04d}"

        # Learning note must be generated BEFORE the atomic balance+status
        # update below -- it's an async AI call, and holding a DB
        # transaction open across network I/O would block every other DB
        # operation app-wide for the duration. See test_signal_repo.py's
        # close_signal_with_balance_update() docstring.
        learning_note = await self._generate_learning_note(signal_id, outcome, pnl_pts, net_pnl_dol)

        new_balance = tdb.close_signal_with_balance_update(
            signal_id, outcome, close_px, net_pnl_pts, net_pnl_dol,
            ref, direction, learning_note=learning_note,
        )

        _log.info(
            "[TestSignal] %s %s CLOSED %s gross_pts=%.2f net_pts=%.2f pnl_$=%.2f balance=$%.2f",
            ref, direction, outcome.upper(), pnl_pts, net_pnl_pts, net_pnl_dol, new_balance,
        )

        try:
            from backend.src.controllers.sync.ledger import push_trade_closed
            _sig_for_ledger = tdb.get_signal_by_id(signal_id)
            push_trade_closed({
                "trade_id":    ref,
                "engine":      "bounce",
                "direction":   direction,
                "strategy":    (_sig_for_ledger or {}).get("strategy", ""),
                "open_time":   (_sig_for_ledger or {}).get("created_at"),
                "close_time":  time.time(),
                "tg_source":   "Bounce Generator",
                "mt5_ticket":  (_sig_for_ledger or {}).get("mt5_ticket"),
                "pnl_dollars": net_pnl_dol,
                "outcome":     outcome,
            })
        except Exception as _le:
            _log.debug("[Ledger] push failed: %s", _le)

        # If live execution was used, resolve the actual MT5 P&L immediately.
        _mt5_outcome_resolved = False
        sig_row = tdb.get_signal_by_id(signal_id)
        mt5_ticket = sig_row.get("mt5_ticket") if sig_row else None
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
                    outcome = (
                        "win"  if actual_profit > 1.0  else
                        "loss" if actual_profit < -1.0 else
                        "be"
                    )
                    tdb.update_signal_pnl_from_mt5(signal_id, actual_profit, outcome)
                    _mt5_outcome_resolved = True
                    _log.debug(
                        "[TestSignal] SIG-%04d resolved to MT5 profit $%.2f (%s)",
                        signal_id, actual_profit, outcome,
                    )
            except Exception as _e:
                _log.debug("[TestSignal] MT5 P&L lookup failed for sig %d: %s", signal_id, _e)

        # ML outcome: re-classify as "loss" if virtual costs flip a marginal win/be
        # into a net loss — but only when the outcome is not already grounded in
        # the real MT5 profit.
        ml_outcome = outcome
        if not _mt5_outcome_resolved and outcome in ("win", "be") and net_pnl_pts <= 0:
            ml_outcome = "loss"
        ml.record_outcome(signal_id, ml_outcome)

        self._closed_trade_count += 1

        if self._closed_trade_count % _BATCH_REVIEW_EVERY == 0:
            asyncio.create_task(self._run_batch_analysis())

    async def _manage_triggered_signal(self, sig: dict, bid: float, ask: float, now: float,
                                       spread_raw: float) -> bool:
        """The conservative-strategy override, early-BE, time-stop, SL, TP1,
        and TP3 ladder for a triggered, non-live-managed signal -- extracted
        from _check_outcomes. Returns True if this call closed or modified
        the signal (caller uses this to decide whether to set notify=True).
        Caller (_check_outcomes in test_signal_service.py) has already
        handled: live-execution closure sync, expiry, and pending->triggered
        zone-dwell logic.

        Stays async and awaits _close_signal directly (not
        asyncio.ensure_future/fire-and-forget) -- the original inline code
        in _check_outcomes awaited each close sequentially before moving to
        the next signal in the loop; firing-and-forgetting here would change
        that ordering/concurrency behavior, which this extraction must not
        do silently."""
        sid       = sig["id"]
        direction = sig["direction"].upper()
        sl        = float(sig["stop_loss"] or 0)
        tp1       = sig.get("tp1")
        tp3       = sig.get("tp3")
        entry_mid = float(sig["entry_mid"] or 0)
        lot_size  = float(sig.get("lot_size") or _MIN_LOT)
        sl_moved  = bool(sig.get("sl_moved_to_be"))
        tp1_moved_this_call = False

        current = bid if direction == "BUY" else ask
        trigger_price = float(sig.get("trigger_price") or entry_mid)
        trigger_time = float(sig.get("trigger_time") or 0)
        strategy = (sig.get("strategy") or "be_runner").lower()

        # Conservative strategy uses fixed-point TP1/SL from the actual fill
        # price, ignoring the ATR-based levels set at signal creation.
        #   conservative:       SL 5pt / TP1 3pt
        #   conservative_trial: SL 5pt / TP1 5pt  (unchanged — trial keeps 5pt)
        if strategy == "conservative":
            _CO_SL_PT, _CO_TP1_PT = 5.0, 3.0
        else:  # conservative_trial
            _CO_SL_PT, _CO_TP1_PT = 5.0, 5.0
        if strategy in _CONSERVATIVE_STRATS and trigger_price:
            if direction == "BUY":
                tp1 = str(trigger_price + _CO_TP1_PT)
                sl  = trigger_price - _CO_SL_PT
            else:
                tp1 = str(trigger_price - _CO_TP1_PT)
                sl  = trigger_price + _CO_SL_PT
            _db_sl  = float(sig.get("stop_loss") or 0)
            _db_tp1 = float(sig.get("tp1") or 0)
            if abs(_db_sl - sl) > 0.001 or abs(_db_tp1 - float(tp1)) > 0.001:
                tdb.update_conservative_levels(sid, sl, float(tp1))

        # ── Early breakeven + time stop (payoff-ratio repair) ─────────────
        _sig_regime = sig.get("regime") or None
        if strategy not in _CONSERVATIVE_STRATS and trigger_price:
            _unreal = round(
                (current - trigger_price) if direction == "BUY"
                else (trigger_price - current), 2,
            )
            _sl_dist_v = float(sig.get("sl_dist") or 0) or abs(trigger_price - sl)

            if (not sl_moved and _sl_dist_v > 0
                    and _unreal >= _sl_dist_v * ap.get("early_be_frac", regime=_sig_regime)):
                _be_cost = _compute_cost_pts(spread_raw)
                _be_px = round(
                    trigger_price + _be_cost if direction == "BUY"
                    else trigger_price - _be_cost, 2,
                )
                tdb.set_signal_sl_moved(sid, _be_px)
                sl = _be_px
                sl_moved = True
                _log.info(
                    "[TestSignal] SIG-%04d early BE @ +%.1fpts (%.0f%% of SL dist)",
                    sid, _unreal, ap.get("early_be_frac", regime=_sig_regime) * 100,
                )
                return True

            _ts_mins = ap.get("time_stop_mins", regime=_sig_regime)
            if (trigger_time > 0 and (now - trigger_time) > _ts_mins * 60
                    and _unreal < 1.0):
                close_px = current
                outcome = "loss" if _unreal < -1.0 else "be"
                _log.info(
                    "[TestSignal] SIG-%04d time stop after %.0fmin @ %+.1fpts → %s",
                    sid, (now - trigger_time) / 60, _unreal, outcome,
                )
                await self._close_signal(
                    sid, outcome, close_px, _unreal, lot_size, direction, spread=spread_raw
                )
                return True

        # ── SL hit ────────────────────────────────────────────────────────
        if sl > 0:
            sl_hit = (direction == "BUY" and bid <= sl) or (direction == "SELL" and ask >= sl)
            if sl_hit:
                close_px = sl
                pnl_pts  = round(sl - trigger_price, 2) if direction == "BUY" else round(trigger_price - sl, 2)
                outcome  = "be" if sl_moved else "loss"
                await self._close_signal(
                    sid, outcome, close_px, pnl_pts, lot_size, direction, spread=spread_raw
                )
                return True

        # ── TP1 hit ───────────────────────────────────────────────────────
        if tp1 and not sl_moved:
            tp1f    = float(tp1)
            tp1_hit = (direction == "BUY" and bid >= tp1f) or (direction == "SELL" and ask <= tp1f)
            if tp1_hit:
                if strategy in _CONSERVATIVE_STRATS:
                    close_px = tp1f
                    pnl_pts  = round(tp1f - trigger_price, 2) if direction == "BUY" else round(trigger_price - tp1f, 2)
                    await self._close_signal(
                        sid, "win", close_px, pnl_pts, lot_size, direction, spread=spread_raw
                    )
                    return True
                else:
                    # be_runner / scale_out: TP1 hit moves SL to break-even
                    # PLUS the round-trip cost.
                    _be_cost = _compute_cost_pts(spread_raw)
                    _be_px = round(
                        trigger_price + _be_cost if direction == "BUY"
                        else trigger_price - _be_cost, 2,
                    )
                    tdb.set_signal_sl_moved(sid, _be_px)
                    sl_moved = True
                    tp1_moved_this_call = True
                    # Fall through — TP3 check may also fire this cycle.

        # ── TP3 hit — full winner (be_runner after TP1, or direct TP3 hit) ─
        if tp3 and strategy not in _CONSERVATIVE_STRATS:
            tp3f    = float(tp3)
            tp3_hit = (direction == "BUY" and bid >= tp3f) or (direction == "SELL" and ask <= tp3f)
            if tp3_hit:
                close_px = tp3f
                pnl_pts  = round(tp3f - trigger_price, 2) if direction == "BUY" else round(trigger_price - tp3f, 2)
                await self._close_signal(
                    sid, "win", close_px, pnl_pts, lot_size, direction, spread=spread_raw
                )
                return True

        return tp1_moved_this_call  # True only if TP1 fired THIS call (not a stale prior-cycle flag)
