"""Live (demo/real) MT5 order dispatch for a triggered Breakout signal --
extracted verbatim (no logic changes) from engine.py's _execute_live as
part of task 030. See docs/todo/refactor/breakout-signal-migration/030-*.md.

_LiveExecuteMixin is composed into BreakoutEngine (breakout_signal_service.py).

Real-money surface: this is the one path in breakout_signal that can place
an actual MT5 order.
"""
from __future__ import annotations

import logging
import time

from forex_trader.breakout_signal import breakout_signal_repo as bdb
from forex_trader.core.momentum_exhaustion import check_momentum_exhaustion

_log = logging.getLogger("breakout_signal")


class _LiveExecuteMixin:
    async def _execute_live(self, sig: dict, fill_px: float, tick) -> None:
        """Place a real MT5 trade via the main engine when bo_live_execution is ON."""
        sig_id     = sig.get("id")
        signal_ref = sig.get("signal_ref") or f"BO-{sig_id or 0:04d}"
        direction  = (sig.get("direction") or "").upper()

        if self._main_engine is None:
            _log.warning("[BO-LiveExec] %s skipped — main engine not linked", signal_ref)
            if sig_id:
                bdb.update_live_exec_result(sig_id, None, None, "skipped:no_main_engine")
            return

        from forex_trader.core import database as _cdb
        rs = _cdb.get_risk_settings()
        if not bool(rs.get("bo_live_execution", 0)):
            if sig_id:
                bdb.update_live_exec_result(sig_id, None, None, "skipped:live_off")
            return

        # Trading Schedule gate, Breakout Engine source (2026-07-24) -- each
        # of the 7x3 windows independently allows/blocks this engine rather
        # than one blanket automated-order switch. See core_trading_schedule.py.
        from forex_trader.core.core_trading_schedule import check_trading_schedule
        _sched_ok, _sched_reason = check_trading_schedule(source="breakout_engine")
        if not _sched_ok:
            _log.info("[BO-LiveExec] %s skipped -- %s", signal_ref, _sched_reason)
            if sig_id:
                bdb.update_live_exec_result(sig_id, None, None, f"skipped:schedule:{_sched_reason}")
            return

        if bool(rs.get("hour_blocklist_enabled", 0)):
            from datetime import datetime as _dt, timezone as _tz
            from forex_trader.core.regime import BREAKOUT_BLOCKED_HOURS_UTC
            _hour_now = _dt.now(_tz.utc).hour
            if _hour_now in BREAKOUT_BLOCKED_HOURS_UTC:
                reason = f"hour_blocklist: {_hour_now:02d} UTC (measured -$1,079 in 12-14 window)"
                _log.info("[BO-LiveExec] %s skipped — %s", signal_ref, reason)
                if sig_id:
                    bdb.update_live_exec_result(sig_id, None, None, f"skipped:{reason}")
                return

        # Fill-time re-evaluation (2026-07-28) -- a pending breakout signal
        # can sit anywhere from a few seconds (the common case) to hours
        # (held by a closed Trading Markets session) before this fires, but
        # until now nothing re-checked current price action or re-scored the
        # ML prediction before firing -- both were trusted exactly as they
        # were at signal creation, however stale. Mirrors Reversal Engine's
        # own fill-time re-evaluation (reversal_engine_live_execute.py).
        # Falls back to the creation-time ml_prob if the refresh itself
        # fails (bridge offline, etc.) rather than skipping the gates.
        ml_prob = sig.get("ml_prob")
        try:
            m5_fresh = await self._bridge.get_candles("M5", 80)
            h1_fresh = await self._bridge.get_candles("H1", 120)
            h4_fresh = await self._bridge.get_candles("H4", 40)
            if m5_fresh and h1_fresh:
                from forex_trader.core.dpm_engine import compute_atr
                from forex_trader.breakout_signal.signal_generator import (
                    compute_htf_bias, compute_h4_bias, compute_adx,
                    compute_macd_hist, get_session,
                )
                fresh_atr  = compute_atr(m5_fresh[-20:], period=14)
                fresh_adx  = compute_adx(m5_fresh[-30:] if len(m5_fresh) >= 30 else m5_fresh)
                fresh_htf  = compute_htf_bias(h1_fresh)
                fresh_h4   = compute_h4_bias(h4_fresh) if h4_fresh else "neutral"
                fresh_closes = [float(c.get("close", 0) or 0) for c in m5_fresh]
                _, fresh_macd = compute_macd_hist(fresh_closes)
                fresh_session = get_session()

                # Momentum-exhaustion / rejection re-check -- catches the
                # exact failure mode a stale signal risks: the market already
                # made (or reversed) its move while this signal was waiting.
                # Deliberately a fast local check, not an ML/AI call -- see
                # core/momentum_exhaustion.py's own docstring for why.
                _mx_ok, _mx_reason = check_momentum_exhaustion(direction, m5_fresh, fresh_atr)
                if not _mx_ok:
                    age_s = time.time() - float(sig.get("created_at", 0) or 0)
                    reason = f"momentum re-check: {_mx_reason} (signal was {age_s:.0f}s old)"
                    _log.info("[BO-LiveExec] %s %s skipped — %s", signal_ref, direction, reason)
                    if sig_id:
                        bdb.update_live_exec_result(sig_id, None, None, f"skipped:{reason}")
                    return

                # Fresh ML re-score -- same feature set ml_engine.extract_features
                # used at creation, with every dynamic input recomputed against
                # now instead of signal-creation time. Static fields (rr_tp1,
                # sl_dist, quality_score, breakout_type, created_at) are left as
                # originally captured -- those describe the signal itself, not
                # current conditions.
                from forex_trader.breakout_signal import ml_engine as bo_ml
                fresh_sig = dict(sig)
                fresh_sig["atr_m15"]       = fresh_atr
                fresh_sig["adx_at_signal"] = fresh_adx
                fresh_sig["htf_bias"]      = fresh_htf
                fresh_sig["h4_bias"]       = fresh_h4
                fresh_sig["macd_hist"]     = fresh_macd
                fresh_sig["session"]       = fresh_session
                try:
                    from forex_trader.core.news_calendar import get_news_proximity_norm as _get_news
                    fresh_sig["news_proximity_norm"] = _get_news()
                except Exception:
                    pass
                try:
                    from forex_trader.core import database as _cdb_fresh
                    fresh_sig["regime_score"]         = _cdb_fresh.get_regime_score(fresh_adx, fresh_atr)
                    fresh_sig["equity_drawdown_pct"]  = _cdb_fresh.get_equity_drawdown_pct()
                    fresh_sig["concurrent_agreement"] = _cdb_fresh.get_concurrent_agreement(
                        "breakout", direction)
                except Exception:
                    pass
                fresh_feats = bo_ml.extract_features(fresh_sig)
                if fresh_feats:
                    _fp = bo_ml.predict(fresh_feats)
                    if _fp is not None:
                        ml_prob = _fp
        except Exception as _refresh_exc:
            _log.warning(
                "[BO-LiveExec] %s fill-time re-evaluation failed, falling back to "
                "creation-time ml_prob: %s", signal_ref, _refresh_exc,
            )

        vantage_sig_id = None
        try:
            from forex_trader.breakout_signal import ml_engine as bo_ml

            base_lot = float(sig.get("lot_size") or 0)

            _BO_ML_R_FLOOR = 0.0
            if ml_prob is not None and bo_ml.has_batch() and float(ml_prob) < _BO_ML_R_FLOOR:
                reason = f"ML gate: predicted R={float(ml_prob):.3f} < 0 (expected loss)"
                _log.info("[BO-LiveExec] %s %s — %s", signal_ref, direction, reason)
                if sig_id:
                    bdb.update_live_exec_result(sig_id, None, None, f"skipped:{reason}")
                return

            if base_lot > 0 and ml_prob is not None and bo_ml.has_batch():
                ml_scale = round(max(0.5, min(1.3, 0.5 + float(ml_prob) * 0.4)), 2)
                base_lot = round(base_lot * ml_scale, 2)

            try:
                if bool(rs.get("kelly_sizing_enabled", 0)) and base_lot > 0:
                    _recent = bdb.get_recent_closed_signals(limit=50)
                    _wins   = [s for s in _recent if s.get("outcome") == "win"]
                    _losses = [s for s in _recent if s.get("outcome") == "loss"]
                    _n      = len(_wins) + len(_losses)
                    if _n >= 20 and _wins and _losses:
                        _avg_w = sum(s.get("pnl_pts", 0) or 0 for s in _wins)  / len(_wins)
                        _avg_l = abs(sum(s.get("pnl_pts", 0) or 0 for s in _losses) / len(_losses))
                        if _avg_l > 0 and _avg_w > 0:
                            _wr = len(_wins) / _n
                            _R  = _avg_w / _avg_l
                            _kf = _wr - (1 - _wr) / _R
                            _hk = _kf / 2
                            _km = round(max(0.75, min(1.25, 1.0 + _hk)), 2)
                            base_lot = round(base_lot * _km, 2)
                            _log.info(
                                "[BO-LiveExec] Kelly: wr=%.0f%% R=%.2f f=%.3f mult=%.2f lot→%.2f",
                                _wr * 100, _R, _kf, _km, base_lot,
                            )
            except Exception as _ke:
                _log.debug("[BO-LiveExec] Kelly sizing error: %s", _ke)

            entry = float(sig.get("entry_mid") or fill_px)
            sl    = float(sig.get("stop_loss") or 0)
            tp1   = sig.get("tp1")
            tp2   = sig.get("tp2")
            tp3   = sig.get("tp3")

            main_sig = self._main_engine.create_signal(
                source_name = "Breakout Engine",
                direction   = direction,
                entry_low   = round(entry - 0.5, 2),
                entry_high  = round(entry + 0.5, 2),
                stop_loss   = sl,
                tp1=tp1, tp2=tp2, tp3=tp3,
                lot_size = base_lot,
                notes    = signal_ref,
            )
            vantage_sig_id = main_sig["signal_id"]
            result = await self._main_engine.open_trade_from_signal(vantage_sig_id, tick=tick)
            mt5_ticket = result.get("mt5_ticket")
            _log.info(
                "[BO-LiveExec] %s %s live trade opened: ticket=%s entry=%.2f",
                signal_ref, direction, mt5_ticket, result.get("entry_price", fill_px),
            )
            if sig_id:
                bdb.update_live_exec_result(sig_id, mt5_ticket, vantage_sig_id, "success")
        except Exception as exc:
            reason = str(exc)[:120]
            _log.warning("[BO-LiveExec] %s live trade failed: %s", signal_ref, reason)
            if sig_id:
                bdb.update_live_exec_result(sig_id, None, vantage_sig_id, f"failed:{reason}")
