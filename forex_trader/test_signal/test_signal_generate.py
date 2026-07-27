"""M15/M5 signal generation cycle for TestSignalEngine (Bounce) -- extracted
verbatim (no logic changes) from test_signal_service.py's _run_cycle as a
second-pass split, since the first cut of test_signal_service.py landed at
1,041 lines (over the 800-line ceiling). See
docs/todo/refactor/test-signal-migration/030-*.md.

_GenerateMixin is composed into TestSignalEngine (test_signal_service.py).
"""
from __future__ import annotations

import logging
import math
import time

from forex_trader.core.dpm_engine import compute_atr

from forex_trader.test_signal import test_signal_repo as tdb
from forex_trader.test_signal import adaptive_params as ap
from forex_trader.test_signal.signal_generator import (
    compute_htf_bias,
    compute_h4_bias,
    compute_adx,
    compute_macd_hist,
    identify_key_levels,
    check_entry_trigger,
    check_scalp_trigger,
    calculate_risk_levels,
    calculate_scalp_risk_levels,
    get_session,
    is_news_window,
)
from forex_trader.test_signal.claude_reviewer import review_signal
from forex_trader.test_signal import ml_engine as ml
from forex_trader.test_signal import market_context as mktctx
from forex_trader.test_signal.test_signal_velocity import _compute_swing_levels

_log = logging.getLogger("test_signal")

_RISK_PCT   = 0.01
_MIN_LOT    = 0.01
_FIXED_LOT  = 0.10
_TS_ML_R_FLOOR      = 0.0
_CONSEC_LOSS_LIMIT  = 3
_CONSEC_LOSS_WINDOW = 7200
_CANDLE_CACHE_MAX_AGE = 25.0


def _fetch_recent_tg_signals(max_age_seconds: int = 7200) -> list[dict]:
    """Return up to 3 Telegram signals from the main DB received in the last max_age_seconds."""
    import time as _t
    try:
        from forex_trader.core import database as _main_db
        cutoff = _t.time() - max_age_seconds
        with _main_db.db() as conn:
            rows = conn.execute(
                "SELECT source_name, direction, entry_low, entry_high, stop_loss, "
                "tp1, tp2, tp3, created_at FROM vantage_signals "
                "WHERE created_at >= ? ORDER BY created_at DESC LIMIT 3",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.debug("[TestSignal] TG signal fetch error: %s", e)
        return []


def _calc_lot_size(
    balance: float,
    sl_dist: float,
    risk_pct: float = _RISK_PCT,
    fixed_lot: float = 0.0,
) -> tuple[float, float]:
    """
    Returns (lot_size, risk_amount) for a XAUUSD virtual trade.

    Priority:
      1. fixed_lot > 0  — use it directly (mirrors main app strategy_lot_size).
      2. Otherwise      — risk-based sizing: risk_pct % of balance / (sl_dist × $100/lot/pt).
    """
    if fixed_lot > 0:
        risk_amount = round(fixed_lot * sl_dist * 100.0, 2)
        return fixed_lot, risk_amount
    risk_amount = round(balance * risk_pct, 2)
    if sl_dist <= 0:
        return _MIN_LOT, risk_amount
    raw = risk_amount / (sl_dist * 100.0)
    lot_size = max(_MIN_LOT, math.floor(raw * 100) / 100)
    return lot_size, risk_amount


class _GenerateMixin:
    async def _run_cycle(self) -> None:
        from forex_trader.core import database as _db_module
        if await _db_module.to_db_thread(_db_module.is_remote_node):
            self._status = "Remote/VPS node — signal generation is local-node-only"
            return
        if not await _db_module.to_db_thread(_db_module.should_generate_signals_here):
            self._status = "Centralized mode: generation runs on the local node"
            return

        emergency = self._force_scan
        self._force_scan = False
        if emergency:
            self._last_emergency_scan = time.time()
        self._status = "analysing"
        log_entry: dict = {"ts": time.time()}

        # ── 2. Suppress conditions ────────────────────────────────────────────
        session = get_session()
        log_entry["session"] = session

        _labeled = len(tdb.get_ml_training_data())
        _bootstrap = _labeled < ml.BOOTSTRAP_SAMPLES

        if session == "closed":
            reason = "Market closed — weekend"
            self._status_detail = reason
            self._status = "running"
            log_entry["suppressed_reason"] = reason
            tdb.log_analysis(log_entry)
            _log.debug(reason)
            return

        _sess_ok, _sess_name = await _db_module.to_db_thread(_db_module.is_session_allowed)
        if not _sess_ok:
            reason = f"Session '{_sess_name}' not enabled in Trading Markets"
            self._status_detail = reason
            self._status = "running"
            log_entry["suppressed_reason"] = reason
            tdb.log_analysis(log_entry)
            _log.debug(reason)
            return

        if is_news_window():
            reason = "News window active — suppressed"
            self._status_detail = reason
            self._status = "running"
            log_entry["suppressed_reason"] = reason
            tdb.log_analysis(log_entry)
            _log.debug(reason)
            return

        # ── 3. Fetch market data ──────────────────────────────────────────────
        try:
            tick = await self._bridge.get_tick()
            if not tick:
                self._status_detail = "No tick — bridge offline"
                self._status = "running"
                _log.warning("No tick from bridge")
                return

            _spread = float(tick.ask) - float(tick.bid)
            _max_spread = ap.get("max_spread_pts")
            if _spread > _max_spread:
                reason = f"Spread {_spread:.2f} > {_max_spread:.2f} limit — suppressed"
                self._status_detail = reason
                self._status = "running"
                log_entry["suppressed_reason"] = reason
                tdb.log_analysis(log_entry)
                _log.debug(reason)
                return

            _cache_age = time.time() - min(
                (v[0] for v in self._cached_candles.values()), default=0.0
            ) if self._cached_candles else float("inf")

            if emergency and _cache_age <= _CANDLE_CACHE_MAX_AGE:
                h1_candles  = self._cached_candles["H1"][1]
                m15_candles = self._cached_candles["M15"][1]
                h4_candles  = self._cached_candles.get("H4", (0, []))[1]
                m5_candles  = self._cached_candles.get("M5", (0, []))[1]
                _log.debug("[TestSignal] Emergency scan using cached candles (age=%.1fs)", _cache_age)
            else:
                h1_candles  = await self._bridge.get_candles("H1",  120)
                m15_candles = await self._bridge.get_candles("M15",  60)
                h4_candles  = await self._bridge.get_candles("H4",   40)
                m5_candles  = await self._bridge.get_candles("M5",   30)
                if h1_candles and m15_candles:
                    _ts = time.time()
                    self._cached_candles = {
                        "H1":  (_ts, h1_candles),
                        "M15": (_ts, m15_candles),
                        "H4":  (_ts, h4_candles or []),
                        "M5":  (_ts, m5_candles or []),
                    }
                    self._last_candle_refresh = _ts

            if not h1_candles or not m15_candles:
                self._status_detail = "No candle data"
                self._status = "running"
                return

        except Exception as e:
            self._status_detail = f"Bridge error: {e}"
            self._status = "running"
            _log.warning("Bridge fetch error: %s", e)
            return

        sh, sl_swing = _compute_swing_levels(m15_candles)
        if sh > 0:
            self._cached_swing_high = sh
        if sl_swing > 0:
            self._cached_swing_low = sl_swing

        # ── 4. Gate on new candle close ───────────────────────────────────────
        latest_m15_time = float(m15_candles[-1].get("ts", 0) or 0)
        latest_m5_time  = float(m5_candles[-1].get("ts", 0) or 0) if m5_candles else 0.0
        is_new_m15 = latest_m15_time > self._last_m15_candle_time
        is_new_m5  = bool(m5_candles) and (latest_m5_time > self._last_m5_candle_time)

        if not emergency and not is_new_m15 and not is_new_m5:
            self._status_detail = "Waiting for next candle close"
            self._status = "running"
            return

        if is_new_m15:
            self._last_m15_candle_time = latest_m15_time
        if is_new_m5:
            self._last_m5_candle_time = latest_m5_time
        if emergency:
            _log.info("[FastMonitor] Emergency scan — candle gate bypassed")

        current_price = float(tick.ask)
        atr_m15 = compute_atr(m15_candles[-20:], period=14)

        log_entry["price"]   = current_price
        log_entry["atr_m15"] = atr_m15

        # ── ATR collapse gate ──────────────────────────────────────────────────
        if len(m15_candles) >= 50:
            _atr_ref = compute_atr(m15_candles[-50:-10], period=14)
            if _atr_ref > 0:
                try:
                    from forex_trader.core import database as _cdb_ac
                    _ac_thr = float(_cdb_ac.get_risk_settings().get("atr_collapse_threshold", 0.65))
                except Exception:
                    _ac_thr = 0.65
                _atr_ratio = atr_m15 / _atr_ref
                if _atr_ratio < _ac_thr:
                    reason = (
                        f"ATR collapse: {atr_m15:.1f} is {_atr_ratio:.0%} of baseline "
                        f"{_atr_ref:.1f} (<{_ac_thr:.0%}) — dead market suppressed"
                    )
                    self._status_detail = reason
                    log_entry.update({"suppressed_reason": reason, "result": "atr_collapse"})
                    tdb.log_analysis(log_entry)
                    _log.debug("[TestSignal] %s", reason)
                    self._status = "running"
                    self._last_cycle_at = time.time()
                    return

        # ── 5. HTF bias + H4 + indicators ────────────────────────────────────
        htf_bias = compute_htf_bias(h1_candles)
        h4_bias  = compute_h4_bias(h4_candles) if h4_candles else "neutral"
        atr_m5   = compute_atr(m5_candles[-15:], period=14) if m5_candles and len(m5_candles) >= 15 else 0.0
        m15_closes_all = [float(c.get("close", 0) or 0) for c in m15_candles]
        adx      = compute_adx(m15_candles)
        _, macd_hist = compute_macd_hist(m15_closes_all)
        _, _macd_3 = compute_macd_hist(m15_closes_all[:-3]) if len(m15_closes_all) > 40 else (0.0, macd_hist)
        _, _macd_6 = compute_macd_hist(m15_closes_all[:-6]) if len(m15_closes_all) > 43 else (0.0, _macd_3)
        macd_hist_trend = [round(_macd_6, 4), round(_macd_3, 4), round(macd_hist, 4)]
        from forex_trader.core.regime import classify_day, efficiency_ratio
        h1_closes = [float(c.get("close", 0) or 0) for c in h1_candles]
        h1_eff    = efficiency_ratio(h1_closes, n=24)
        regime    = classify_day(adx, h1_eff)
        log_entry["htf_bias"] = htf_bias
        log_entry["h4_bias"]  = h4_bias
        log_entry["adx"]      = round(adx, 1)
        log_entry["h1_eff"]   = h1_eff
        log_entry["regime"]   = regime

        # ── 6. Key levels ─────────────────────────────────────────────────────
        key_levels = identify_key_levels(h1_candles, current_price, m15_candles=m15_candles)
        log_entry["key_levels"] = key_levels[:8]

        # ── 7. Entry trigger ──────────────────────────────────────────────────
        candidate = check_entry_trigger(
            m15_candles, h1_candles, key_levels, htf_bias, current_price, atr_m15,
            h4_bias=h4_bias, regime=regime, macd_hist=macd_hist, session=session,
        )
        if not candidate and m5_candles and atr_m5 > 0:
            candidate = check_scalp_trigger(
                m5_candles, key_levels, htf_bias, current_price, atr_m5, session
            )
        log_entry["candidate"] = candidate

        if not candidate:
            reason = "No entry trigger — no level touch with candle confirmation"
            self._status_detail = reason
            log_entry["suppressed_reason"] = reason
            log_entry["result"] = "no_trigger"
            tdb.log_analysis(log_entry)
            self._status = "running"
            self._last_cycle_at = time.time()
            return

        candidate.setdefault("h4_bias",   h4_bias)
        candidate.setdefault("regime",    regime)
        candidate.setdefault("macd_hist", macd_hist)
        candidate.setdefault("adx",       adx)

        # ── 7a2. Extreme-trend gate for mean-reversion patterns ────────────────
        _extreme_adx = ap.get("extreme_trend_adx")
        _extreme_trend = (
            htf_bias != "neutral" and h4_bias == htf_bias and adx >= _extreme_adx
        )
        if _extreme_trend and candidate.get("trigger_pattern") in ("bounce", "liquidity_sweep"):
            reason = (
                f"Extreme trend block: H1+H4 both {htf_bias}, ADX {adx:.0f} >= {_extreme_adx:.0f} "
                f"— {candidate['trigger_pattern']} pattern unsafe (levels don't hold in persistent trends)"
            )
            self._status_detail = reason
            log_entry["suppressed_reason"] = reason
            log_entry["result"] = "extreme_trend_block"
            tdb.log_analysis(log_entry)
            _log.debug("[TestSignal] %s", reason)
            self._status = "running"
            self._last_cycle_at = time.time()
            return

        # ── 7b. Dual-bias counter-trend gate ──────────────────────────────────
        _db_threshold = ap.get("dual_bias_adx_block")
        _dual_bias_trending = (
            htf_bias != "neutral"
            and h4_bias == htf_bias
            and adx >= _db_threshold
        )
        if _dual_bias_trending:
            _is_counter = (
                (candidate["direction"] == "BUY"  and htf_bias == "bearish")
                or (candidate["direction"] == "SELL" and htf_bias == "bullish")
            )
            if _is_counter and candidate.get("trigger_pattern") != "liquidity_sweep":
                reason = (
                    f"Dual-bias block: H1+H4 both {htf_bias}, ADX {adx:.0f} ≥ {_db_threshold:.0f} "
                    f"— counter-trend {candidate['direction']} blocked"
                )
                self._status_detail = reason
                log_entry["suppressed_reason"] = reason
                log_entry["result"] = "dual_bias_block"
                tdb.log_analysis(log_entry)
                _log.debug("[TestSignal] %s", reason)
                self._status = "running"
                self._last_cycle_at = time.time()
                return

        # ── 7c. Asian session: trend-aligned signals only ─────────────────────
        if session == "asian" and htf_bias != "neutral":
            _asian_counter = (
                (candidate["direction"] == "BUY"  and htf_bias == "bearish")
                or (candidate["direction"] == "SELL" and htf_bias == "bullish")
            )
            if _asian_counter and candidate.get("trigger_pattern") != "liquidity_sweep":
                reason = (
                    f"Asian counter-bias block: HTF {htf_bias}, signal {candidate['direction']} — "
                    "only trend-aligned signals in Asian session"
                )
                self._status_detail = reason
                log_entry["suppressed_reason"] = reason
                log_entry["result"] = "asian_bias_block"
                tdb.log_analysis(log_entry)
                _log.debug("[TestSignal] %s", reason)
                self._status = "running"
                self._last_cycle_at = time.time()
                return

        # ── 8. Risk levels ────────────────────────────────────────────────────
        direction = candidate["direction"]
        if candidate.get("is_scalp"):
            risk = calculate_scalp_risk_levels(candidate, atr_m5)
        else:
            risk = calculate_risk_levels(candidate, atr_m15, key_levels, direction, regime=regime)
        if not risk:
            reason = f"Risk calc failed — R:R below minimum for {direction}"
            self._status_detail = reason
            log_entry["suppressed_reason"] = reason
            log_entry["result"] = "rr_rejected"
            tdb.log_analysis(log_entry)
            self._status = "running"
            self._last_cycle_at = time.time()
            return

        candidate.update(risk)

        # ── 9a. Consecutive-loss direction cooldown ───────────────────────────
        # Uses the named repo function (030), replacing engine.py's raw
        # `with tdb._conn() as _cl_con:` bypass.
        try:
            _cl_outcomes = tdb.get_recent_outcomes_by_direction(
                direction, time.time() - _CONSEC_LOSS_WINDOW, _CONSEC_LOSS_LIMIT
            )
            if len(_cl_outcomes) >= _CONSEC_LOSS_LIMIT and all(o == "loss" for o in _cl_outcomes):
                reason = (
                    f"Consecutive-loss cooldown: {_CONSEC_LOSS_LIMIT} consecutive {direction} "
                    f"losses in last {_CONSEC_LOSS_WINDOW // 3600}h — direction paused"
                )
                self._status_detail = reason
                log_entry["suppressed_reason"] = reason
                log_entry["result"] = "consec_loss_cooldown"
                tdb.log_analysis(log_entry)
                _log.info("[TestSignal] %s", reason)
                self._status = "running"
                self._last_cycle_at = time.time()
                return
        except Exception as _cl_err:
            _log.debug("[TestSignal] Consecutive-loss check error: %s", _cl_err)

        # ── 9b. ML feature extraction ─────────────────────────────────────────
        try:
            from forex_trader.core import database as _cdb_ctx
            from forex_trader.core.news_calendar import get_news_proximity_norm
            candidate["news_proximity_norm"]  = get_news_proximity_norm()
            candidate["equity_drawdown_pct"]  = _cdb_ctx.get_equity_drawdown_pct()
            candidate["concurrent_agreement"] = _cdb_ctx.get_concurrent_agreement("bounce", direction)
        except Exception:
            pass

        _market_ctx = mktctx.get_context()
        ml_features = ml.extract_features(
            m15_candles, h1_candles, candidate, key_levels, session, htf_bias, atr_m15,
            h4_bias=h4_bias, adx=adx, macd_hist=macd_hist, market_ctx=_market_ctx,
        )
        ml_pred = ml.predict(ml_features) if ml_features else None
        if ml_pred is not None:
            log_entry["ml_prob"] = round(float(ml_pred), 3)
            _log.debug("[TestSignal] ML predicted_R=%.3f", ml_pred)

        # ── 9c. ML R-multiple gate — block signals with negative predicted R ──
        if ml_pred is not None and ml.is_trained() and float(ml_pred) < _TS_ML_R_FLOOR and not _bootstrap:
            reason = f"ML gate: predicted_R={ml_pred:.3f} < {_TS_ML_R_FLOOR:.2f} floor"
            log_entry.update({"result": "ml_gate", "suppressed_reason": reason})
            tdb.log_analysis(log_entry)
            _log.info("[TestSignal] %s — skipping signal", reason)
            return

        # ── DXY opposing gate ─────────────────────────────────────────────────
        _dxy = _market_ctx.get("dxy_momentum", 0.0)
        _dxy_opposes = (
            (direction == "BUY"  and _dxy >  0.4) or
            (direction == "SELL" and _dxy < -0.4)
        )
        if _dxy_opposes and ml_pred is not None and ml.is_trained() and float(ml_pred) < 0.5 and not _bootstrap:
            reason = (
                f"DXY opposing gate: dxy_momentum={_dxy:+.2f} opposes {direction} — "
                f"ML predicted_R={ml_pred:.3f} < 0.5 required when DXY headwind active"
            )
            self._status_detail = reason
            log_entry.update({"result": "dxy_gate", "suppressed_reason": reason})
            tdb.log_analysis(log_entry)
            _log.info("[TestSignal] %s", reason)
            self._status = "running"
            self._last_cycle_at = time.time()
            return

        # ── 10. Claude quality gate (optional) ───────────────────────────────
        try:
            import forex_trader.core.database as _main_db_sg
            _sg_claude_on = bool(_main_db_sg.get_risk_settings().get("sg_claude_eval_enabled", 1))
        except Exception:
            _sg_claude_on = True

        # ── 9. Duplicate guard ────────────────────────────────────────────────
        open_sigs = tdb.get_open_signals()
        for sig in open_sigs:
            if (sig["direction"] == direction
                    and abs(sig["entry_mid"] - risk["entry_mid"]) < 30.0):
                reason = f"Duplicate: open {direction} signal near {sig['entry_mid']:.0f}"
                self._status_detail = reason
                log_entry["suppressed_reason"] = reason
                log_entry["result"] = "duplicate"
                tdb.log_analysis(log_entry)
                self._status = "running"
                self._last_cycle_at = time.time()
                return

        # ── Cross-engine conflict suppression ─────────────────────────────────
        try:
            from forex_trader.core import database as _cdb_bus
            if _cdb_bus.has_conflict_on_bus("bounce", direction, window_seconds=21600.0):
                reason = f"Cross-engine conflict: opposite-direction signal active on bus for {direction}"
                self._status_detail = reason
                log_entry["suppressed_reason"] = reason
                log_entry["result"] = "conflict_suppressed"
                tdb.log_analysis(log_entry)
                _log.info("[TestSignal] %s", reason)
                self._status = "running"
                self._last_cycle_at = time.time()
                return
        except Exception:
            pass

        if _sg_claude_on:
            import backend.src.config as cfg_module
            cfg        = cfg_module.load()
            m15_closes = [float(c.get("close", 0) or 0) for c in m15_candles[-10:]]
            tg_context = _fetch_recent_tg_signals()
            _t0_claude = time.time()
            review = await review_signal(
                candidate, htf_bias, session, key_levels, m15_closes, cfg,
                tg_signals=tg_context,
                ml_prob=ml_pred,
                trigger_pattern=candidate.get("trigger_pattern", "bounce"),
                h4_bias=h4_bias,
                adx=adx,
                macd_hist=macd_hist,
                macd_hist_trend=macd_hist_trend,
                regime=regime,
            )
            _log.debug("[TestSignal] Claude gate took %.2fs", time.time() - _t0_claude)
            log_entry["claude_decision"] = review.get("rationale", "")
            _log.info("[TestSignal] Claude: approved=%s score=%.2f conf=%s — %s",
                      review.get("approved"), review.get("quality_score", 0),
                      review.get("confidence"), review.get("rationale"))
        else:
            review = {
                "approved": True, "quality_score": 0.70, "confidence": "low",
                "rationale": "Claude eval disabled — ML+rules passed",
                "_is_fallback": False,
            }
            log_entry["claude_decision"] = review["rationale"]
            _log.info("[TestSignal] Claude eval OFF — auto-approved")

        if not review.get("approved", True):
            if _bootstrap:
                _log.info(
                    "[TestSignal] Bootstrap: Claude rejected but overriding "
                    "(%d/%d samples). Rationale: %s",
                    _labeled, ml.BOOTSTRAP_SAMPLES, review.get("rationale"),
                )
                log_entry["claude_decision"] = (
                    "[BOOTSTRAP-OVERRIDE] " + review.get("rationale", "")
                )
            else:
                reason = f"Claude rejected: {review.get('rationale', '')}"
                self._status_detail = reason
                log_entry["suppressed_reason"] = reason
                log_entry["result"] = "claude_rejected"
                tdb.log_analysis(log_entry)
                _log.info("[TestSignal] Signal rejected by Claude: %s", review.get("rationale"))
                self._status = "running"
                self._last_cycle_at = time.time()
                return

        quality_score = float(review.get("quality_score", 0.5))
        min_q = ap.get("min_quality_score", regime=regime)
        if quality_score < min_q and not _bootstrap:
            reason = (f"Quality score {quality_score:.0%} below threshold "
                      f"{min_q:.0%} — skipped")
            self._status_detail = reason
            log_entry["suppressed_reason"] = reason
            log_entry["result"] = "quality_low"
            tdb.log_analysis(log_entry)
            _log.info("[TestSignal] Signal skipped (quality %.2f < %.2f)", quality_score, min_q)
            self._status = "running"
            self._last_cycle_at = time.time()
            return

        # ── 11. Position sizing — mirrors live account (1:500 leverage, Vantage) ──
        balance  = tdb.get_virtual_balance()
        sl_dist  = float(risk.get("sl_dist", atr_m15))

        try:
            from forex_trader.core.database import get_risk_settings
            rs = get_risk_settings()
            active_strategy = rs.get("trade_strategy", "be_runner")
        except Exception:
            active_strategy = "be_runner"

        lot_size, risk_amount = _calc_lot_size(
            balance, sl_dist, risk_pct=_RISK_PCT, fixed_lot=_FIXED_LOT
        )

        # ── 13. Store signal ──────────────────────────────────────────────────
        sig_record = {
            "created_at":      time.time(),
            "direction":       direction,
            "entry_low":       risk["entry_low"],
            "entry_high":      risk["entry_high"],
            "entry_mid":       risk["entry_mid"],
            "stop_loss":       risk["stop_loss"],
            "tp1":             risk.get("tp1"),
            "tp2":             risk.get("tp2"),
            "tp3":             risk.get("tp3"),
            "sl_dist":         risk.get("sl_dist"),
            "rr_tp1":          risk.get("rr_tp1"),
            "rr_tp3":          risk.get("rr_tp3"),
            "session":         session,
            "htf_bias":        htf_bias,
            "atr_m15":         atr_m15,
            "key_level":       candidate.get("key_level"),
            "key_level_type":  candidate.get("key_level_type"),
            "rationale":       review.get("rationale", ""),
            "quality_score":   quality_score,
            "claude_approved":  review.get("approved", True),
            "claude_fallback":  1 if review.get("_is_fallback") else 0,
            "lot_size":         lot_size,
            "risk_amount":      risk_amount,
            "trigger_pattern":  candidate.get("trigger_pattern", ""),
            "strategy":         active_strategy,
            "ml_prob":          ml_pred,
            "regime":           candidate.get("regime", regime),
            "adx":              candidate.get("adx", adx),
        }
        signal_id, signal_ref = tdb.insert_signal(sig_record)

        if ml_features:
            tdb.store_ml_features(signal_id, ml_features)

        try:
            from forex_trader.core import database as _cdb_bus2
            _cdb_bus2.write_signal_bus(
                "bounce", direction,
                confidence=float(ml_pred) if ml_pred is not None else 0.5,
                signal_id=signal_id,
                ttl_seconds=21600.0,
            )
        except Exception:
            pass

        log_entry["result"] = f"signal_created {signal_ref}"
        tdb.log_analysis(log_entry)

        _log.info(
            "[TestSignal] NEW SIGNAL %s %s @ %.2f–%.2f SL=%.2f TP1=%.2f TP3=%.2f "
            "R:R=%.2f lot=%.2f risk=$%.2f session=%s bias=%s strategy=%s",
            signal_ref, direction,
            risk["entry_low"], risk["entry_high"],
            risk["stop_loss"], risk.get("tp1", 0), risk.get("tp3", 0),
            risk.get("rr_tp1", 0), lot_size, risk_amount,
            session, htf_bias, active_strategy,
        )
        self._status_detail = (
            f"{signal_ref} {direction} @ {risk['entry_mid']:.2f} "
            f"lot={lot_size} risk=${risk_amount:.2f}"
        )
        self._status = "running"
        self._last_cycle_at = time.time()
        self._notify_refresh()
