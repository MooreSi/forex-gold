"""Live (demo/real) MT5 order dispatch for a triggered GD Copy signal --
extracted verbatim (no logic changes) from engine.py's _try_live_execute as
part of task 040. See docs/todo/refactor/backend-foundation/040-*.md.

Kept as its own file per backend-conventions' decomposition pattern --
"the writes: dispatch/submit" get their own file, separate from the
management/correlation concerns. _LiveExecuteMixin is composed into
GDCopyEngine (gd_copy_signal_service.py).

Real-money surface: this is the one path in gd_copy_signal that can place
an actual MT5 order. Task 050 (demo-account validation) is the only task
in this pack allowed to exercise it against a live connection.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from forex_trader.gd_copy_signal import gd_copy_signal_repo as gdc_db
from forex_trader.gd_copy_signal import level_detector as ld

_log = logging.getLogger("gd_copy_signal")

_ML_BLOCK_THRESHOLD = 0.0     # block live execution if predicted R-multiple < 0


class _LiveExecuteMixin:
    async def _try_live_execute(self, sig: dict, trigger_price: float, tick=None) -> None:
        """Execute a real MT5 trade if live execution is enabled and ML gate passes."""
        try:
            from forex_trader.core import database as core_db
            rs = core_db.get_risk_settings()
            if not rs.get("gdc_live_execution", 0):
                # Write a status so the orphan watchdog doesn't misfire on healthy
                # virtual signals when live execution is deliberately disabled.
                gdc_db.update_live_exec(sig["id"], status="skipped:live_disabled")
                return

            # Fill-time re-evaluation (2026-07-17) -- a pending zone signal can
            # sit anywhere from a couple of minutes to 4h (GD VIP Runner/
            # Adaptive Runner's expiry window) before price actually reaches
            # its entry zone, but until now nothing re-checked the market
            # bias or re-scored the ML prediction against CURRENT conditions
            # before firing -- both were trusted exactly as they were at
            # signal creation, however stale. Falls back to the creation-time
            # ml_prob/htf_bias if the refresh itself fails (bridge offline,
            # etc.) rather than skipping the gates entirely.
            fresh_prob = sig.get("ml_prob")
            direction  = sig.get("direction")
            try:
                h1_candles  = await self._bridge.get_candles("H1", 50)
                m15_candles = await self._bridge.get_candles("M15", 80)
                h4_candles  = await self._bridge.get_candles("H4", 6)
                if h1_candles:
                    fresh_htf = ld.get_htf_bias(h1_candles, h4_candles)
                    fresh_atr = self._calc_atr(m15_candles or h1_candles)
                    fresh_adx = self._calc_adx(h1_candles)
                    fresh_session = ld.get_session(datetime.now(timezone.utc).hour)

                    # Bias re-check -- the exact same "against-bias needs a
                    # strong level score" rule _run_cycle already applies at
                    # creation time, just re-evaluated against the CURRENT
                    # bias instead of whatever it was when the signal fired.
                    level_score = float(sig.get("level_score", 0.5) or 0.5)
                    if ((fresh_htf == "bullish" and direction == "SELL" and level_score < 0.75)
                            or (fresh_htf == "bearish" and direction == "BUY" and level_score < 0.75)):
                        gdc_db.store_ml_prob_at_fill(sig["id"], fresh_prob or 0.0, fresh_htf)
                        gdc_db.update_live_exec(sig["id"], status="bias_skipped")
                        _log.info(
                            "[GDC-Engine] bias gate blocked live exec %s -- htf now %s vs "
                            "direction=%s, level_score=%.2f < 0.75",
                            sig.get("signal_ref"), fresh_htf, direction, level_score,
                        )
                        return

                    # Fresh ML re-score -- same feature set ml_engine.
                    # extract_features used at creation, with every dynamic
                    # input (bias/ATR/ADX/session/news/regime/drawdown/
                    # agreement/VIP cadence) recomputed against now instead
                    # of signal-creation time. Static fields (level_type,
                    # level_score, distance, rr_tp1, created_at) are left as
                    # originally captured -- those describe the signal itself,
                    # not current conditions.
                    fresh_sig = dict(sig)
                    fresh_sig["htf_bias"] = fresh_htf
                    fresh_sig["h1_bias"]  = fresh_htf
                    fresh_sig["adx"]      = fresh_adx
                    fresh_sig["atr"]      = fresh_atr
                    fresh_sig["session"]  = fresh_session
                    try:
                        from forex_trader.core.news_calendar import get_news_proximity_norm as _get_news
                        fresh_sig["news_proximity_norm"] = _get_news()
                    except Exception:
                        pass
                    try:
                        fresh_sig["regime_score"] = core_db.get_regime_score(fresh_adx, fresh_atr)
                        fresh_sig["equity_drawdown_pct"] = core_db.get_equity_drawdown_pct()
                        fresh_sig["concurrent_agreement"] = core_db.get_concurrent_agreement(
                            "gd_copy", direction)
                    except Exception:
                        pass
                    try:
                        from forex_trader.gd_copy_signal import ml_engine as gdc_ml
                        fresh_sig["vip_discipline_score"], fresh_sig["vip_aggression_score"] = \
                            gdc_ml.get_daily_research_scores()
                    except Exception:
                        pass
                    cadence = await self._vip_cadence_stats()
                    fresh_sig["minutes_since_last_vip"] = cadence[0]
                    fresh_sig["vip_signals_today"]      = cadence[1]

                    from forex_trader.gd_copy_signal import ml_engine as gdc_ml
                    win_rate = gdc_db.get_recent_win_rate(20)
                    fresh_feats = gdc_ml.extract_features(fresh_sig, win_rate)
                    if fresh_feats:
                        _fp = gdc_ml.predict(fresh_feats)
                        if _fp is not None:
                            fresh_prob = _fp
                    gdc_db.store_ml_prob_at_fill(sig["id"], fresh_prob or 0.0, fresh_htf)
            except Exception as _refresh_exc:
                _log.warning(
                    "[GDC-Engine] fill-time re-evaluation failed for %s, falling back to "
                    "creation-time ml_prob: %s", sig.get("signal_ref"), _refresh_exc,
                )

            # ML gate: block live execution when predicted R-multiple < 0 (expected loss)
            if fresh_prob is not None and float(fresh_prob) < _ML_BLOCK_THRESHOLD:
                gdc_db.update_live_exec(sig["id"], status="ml_skipped")
                _log.info("[GDC-Engine] ML gate blocked live exec (predicted_R=%.3f)", float(fresh_prob))
                return

            if self._main_eng is None:
                return

            # Persist to vantage_signals first -- open_trade_from_signal looks up
            # the signal by signal_id, it does not accept a Signal object.
            # Strategy is resolved automatically downstream via the channel
            # override lookup for source_name="GD Copy Engine" (see
            # _active_strategy, which mirrors the same lookup).
            main_sig = self._main_eng.create_signal(
                source_name = "GD Copy Engine",
                direction   = sig["direction"],
                entry_low   = sig["entry_low"],
                entry_high  = sig["entry_high"],
                stop_loss   = sig["stop_loss"],
                tp1         = sig.get("tp1"),
                tp2         = sig.get("tp2"),
                tp3         = sig.get("tp3"),
                tp4         = sig.get("tp4"),
                tp5         = sig.get("tp5"),
                tp6         = sig.get("tp6"),
                tp7         = sig.get("tp7"),
                tp8         = sig.get("tp8"),
                notes       = f"GDC {sig.get('signal_ref', '')} level={sig.get('level_type', '')}",
            )
            vantage_sig_id = main_sig["signal_id"]

            trade = await self._main_eng.open_trade_from_signal(vantage_sig_id, tick=tick)
            if trade:
                gdc_db.update_live_exec(
                    sig["id"],
                    mt5_ticket=trade.get("mt5_ticket"),
                    vantage_sig_id=vantage_sig_id,
                    status="executed",
                )
                _log.info("[GDC-Engine] live trade opened mt5=%s", trade.get("mt5_ticket", "?"))
            else:
                gdc_db.update_live_exec(sig["id"], status="open_failed")

        except Exception as exc:
            _log.warning("[GDC-Engine] live exec error: %s", exc)
            try:
                gdc_db.update_live_exec(sig["id"], status=f"error:{exc}")
            except Exception:
                pass
