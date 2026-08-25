"""Live (demo/real) MT5 order dispatch for a triggered Bounce signal --
extracted verbatim (no logic changes) from engine.py's _execute_live as
part of task 030. See docs/todo/refactor/test-signal-migration/030-*.md.

_LiveExecuteMixin is composed into TestSignalEngine (test_signal_service.py).

Real-money surface: this is the one path in test_signal that can place an
actual MT5 order.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.src.services.test_signal import test_signal_repo as tdb
from backend.src.services.test_signal import ml_engine as ml

_log = logging.getLogger("test_signal")


class _LiveExecuteMixin:
    async def _execute_live(self, sig: dict, fill_px: float, tick) -> None:
        """
        Place a real MT5 trade via the main engine when sg_live_execution is ON.
        The main engine handles strategy overrides (e.g. conservative 5pt TP),
        Risk Governor, Telegram notifications, History, and all stats.
        The virtual signal continues to track its own outcome independently.
        """
        sig_id     = sig.get("id")
        signal_ref = sig.get("signal_ref") or f"SIG-{sig_id or 0:04d}"
        direction  = sig["direction"].upper()

        if self._main_engine is None:
            _log.warning("[LiveExec] %s skipped — main engine not linked", signal_ref)
            if sig_id:
                tdb.update_live_exec_result(sig_id, None, None, "skipped:no_main_engine")
            return

        # Toxic-hour blocklist: blocks real money only — the signal above
        # this call still generated and will still track/close/train
        # normally. Trading > Strategy > Risk Settings toggle, shared with
        # the Breakout generator, off by default.
        from backend.src.db import database as _cdb_hour
        _rs_bounce = _cdb_hour.get_risk_settings()

        # Internal Engine Exposure guard (Trading > Strategy) -- OFF by
        # default, in which case this is a no-op. See
        # core_internal_exposure_guard.py for the modes and for the measured
        # reason the default is off.
        from backend.src.services.positions.core_internal_exposure_guard import check_internal_exposure
        _exp_ok, _exp_reason = check_internal_exposure(
            direction, float(sig.get("lot_size") or 0) or 0.01, _rs_bounce,
        )
        if not _exp_ok:
            _log.info("[LiveExec] %s skipped — %s", signal_ref, _exp_reason)
            if sig_id:
                tdb.update_live_exec_result(sig_id, None, None, f"skipped:{_exp_reason}")
            return

        if bool(_rs_bounce.get("hour_blocklist_enabled", 0)):
            from backend.src.utils.regime import BOUNCE_BLOCKED_HOURS_UTC
            _hour_now = datetime.now(timezone.utc).hour
            if _hour_now in BOUNCE_BLOCKED_HOURS_UTC:
                reason = f"hour_blocklist: {_hour_now:02d} UTC (measured negative expectancy)"
                _log.info("[LiveExec] %s skipped — %s", signal_ref, reason)
                if sig_id:
                    tdb.update_live_exec_result(sig_id, None, None, f"skipped:{reason}")
                return

        vantage_sig_id = None
        try:
            # Sort TPs into valid direction order before passing to the main engine.
            _raw = [sig.get(k) for k in ("tp1","tp2","tp3","tp4","tp5","tp6","tp7","tp8")]
            _sorted = sorted((t for t in _raw if t is not None), reverse=(direction == "SELL"))
            while len(_sorted) < 8:
                _sorted.append(None)
            tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8 = _sorted

            # Scale lot size by ML confidence: 0.5x at low confidence, 1.0x at 0.5,
            # up to 1.3x at high confidence. Only applies when model is trained.
            base_lot = float(sig.get("lot_size") or 0)
            if base_lot > 0:
                # Confidence sizing capped to 0.75-1.15x (was 0.5-1.3x): measured
                # dollar PnL was asymmetric vs point PnL, meaning the wider range
                # was systematically up-sizing the losing trades.
                _ml_r = sig.get("ml_prob")  # R-multiple prediction
                if _ml_r is not None and ml.is_trained():
                    ml_scale = round(max(0.75, min(1.15, 0.5 + float(_ml_r) * 0.4)), 2)
                else:
                    _q = sig.get("quality_score")
                    ml_scale = round(max(0.75, min(1.15, 0.5 + float(_q))), 2) if _q is not None else 1.0
                base_lot = round(base_lot * ml_scale, 2)

            # Kelly Criterion fractional sizing — adjusts lot by rolling win-rate edge
            try:
                from backend.src.db import database as _cdb_kelly
                if bool(_cdb_kelly.get_risk_settings().get("kelly_sizing_enabled", 0)):
                    _recent = tdb.get_recent_closed_signals(limit=50)
                    _wins   = [s for s in _recent if s.get("outcome") == "win"]
                    _losses = [s for s in _recent if s.get("outcome") == "loss"]
                    _n      = len(_wins) + len(_losses)
                    if _n >= 20 and _wins and _losses:
                        _avg_w = sum(s.get("pnl_pts", 0) or 0 for s in _wins)  / len(_wins)
                        _avg_l = abs(sum(s.get("pnl_pts", 0) or 0 for s in _losses) / len(_losses))
                        if _avg_l > 0 and _avg_w > 0:
                            _wr  = len(_wins) / _n
                            _R   = _avg_w / _avg_l
                            _kf  = _wr - (1 - _wr) / _R   # Kelly fraction
                            _hk  = _kf / 2                  # half-Kelly for safety
                            _km  = round(max(0.75, min(1.25, 1.0 + _hk)), 2)
                            base_lot = round(base_lot * _km, 2)
                            _log.info(
                                "[LiveExec] Kelly: wr=%.0f%% R=%.2f f=%.3f mult=%.2f lot→%.2f",
                                _wr * 100, _R, _kf, _km, base_lot,
                            )
            except Exception as _ke:
                _log.debug("[LiveExec] Kelly sizing error: %s", _ke)

            main_sig = self._main_engine.create_signal(
                source_name = "Bounce Generator",
                direction   = direction,
                entry_low   = float(sig["entry_low"]),
                entry_high  = float(sig["entry_high"]),
                stop_loss   = float(sig["stop_loss"]),
                tp1=tp1, tp2=tp2, tp3=tp3, tp4=tp4, tp5=tp5, tp6=tp6, tp7=tp7, tp8=tp8,
                lot_size = base_lot,
                notes    = signal_ref,
            )
            vantage_sig_id = main_sig["signal_id"]
            result = await self._main_engine.open_trade_from_signal(vantage_sig_id, tick=tick)
            mt5_ticket = result.get("mt5_ticket")
            _log.info(
                "[LiveExec] %s %s live trade opened: ticket=%s entry=%.2f",
                signal_ref, direction, mt5_ticket, result.get("entry_price", fill_px),
            )
            if sig_id:
                tdb.update_live_exec_result(sig_id, mt5_ticket, vantage_sig_id, "success")
        except Exception as exc:
            reason = str(exc)[:120]
            _log.warning("[LiveExec] %s live trade failed: %s", signal_ref, reason)
            if sig_id:
                tdb.update_live_exec_result(sig_id, None, vantage_sig_id, f"failed:{reason}")
