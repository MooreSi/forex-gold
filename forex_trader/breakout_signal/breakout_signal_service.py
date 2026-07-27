"""
BreakoutEngine — trend-following / breakout signal engine.

This is the thin orchestrator (task 030): lifecycle, the M5 signal-
generation cycle, and the outcome-loop's routing live here; TP/SL/partial
management + live-P&L reconciliation (_ManagementMixin), the real-time
velocity monitor (_VelocityMixin), live order dispatch (_LiveExecuteMixin),
and Claude batch tuning (_LearnMixin) are each their own file. Replaces
engine.py -- see docs/todo/refactor/breakout-signal-migration/030-*.md for
what moved where and why.

Runs automatically alongside the bounce (TestSignal) engine.
NEVER places MT5 orders — virtual tracking only until confidence is established.
NEVER shares data with the bounce engine — separate DB, separate signals, separate ML.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import logging.handlers
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import backend.src.config as cfg_module

from forex_trader.breakout_signal import breakout_signal_repo as bdb
from forex_trader.breakout_signal import adaptive_params as ap
from forex_trader.breakout_signal import ml_engine as bo_ml
from forex_trader.breakout_signal.signal_generator import (
    compute_htf_bias,
    compute_h4_bias,
    compute_adx,
    compute_macd_hist,
    identify_key_levels,
    get_session,
    is_news_window,
    check_breakout_go,
    check_breakout_retest,
    check_liquidity_sweep,
    calculate_breakout_risk_levels,
)
from forex_trader.core.dpm_engine import compute_atr
from forex_trader.breakout_signal.claude_reviewer import review_signal
from forex_trader.breakout_signal.breakout_signal_learn import _LearnMixin
from forex_trader.breakout_signal.breakout_signal_velocity import _VelocityMixin
from forex_trader.breakout_signal.breakout_signal_manage import _ManagementMixin
from forex_trader.breakout_signal.breakout_signal_live_execute import _LiveExecuteMixin

if TYPE_CHECKING:
    from forex_trader.core.mt5_bridge import MT5BridgeClient

_log = logging.getLogger("breakout_signal")

# ── Timing ────────────────────────────────────────────────────────────────────
_CYCLE_INTERVAL    = 60
_OUTCOME_INTERVAL  = 5
_LOT_SIZE          = 0.10
_MAX_SIGNAL_AGE    = 8 * 3600
_BOOTSTRAP_SAMPLES = 20

_LEVEL_COOLDOWN    = 2100
_CONSEC_LOSS_LIMIT  = 3
_CONSEC_LOSS_WINDOW = 7200

_instance: Optional["BreakoutEngine"] = None


def get_instance() -> Optional["BreakoutEngine"]:
    return _instance


def init(bridge: "MT5BridgeClient") -> "BreakoutEngine":
    global _instance
    if _instance is None:
        from backend.src.config import USER_DATA_DIR
        data_dir = USER_DATA_DIR / "data"
        bdb.init(str(data_dir / "breakout_signal.db"))
        bo_ml.init(data_dir)
        _instance = BreakoutEngine(bridge)
    return _instance


class BreakoutEngine(_ManagementMixin, _VelocityMixin, _LiveExecuteMixin, _LearnMixin):
    def __init__(self, bridge: "MT5BridgeClient"):
        self._bridge = bridge

        self.is_running    = False
        self.status        = "stopped"
        self.status_detail = ""
        self.last_cycle_at: Optional[float] = None

        self._task_cycle:    Optional[asyncio.Task] = None
        self._task_outcome:  Optional[asyncio.Task] = None
        self._task_velocity: Optional[asyncio.Task] = None

        self._refresh_cbs: list[Callable] = []

        self._cached: dict = {
            "key_levels": [],
            "adx":        0.0,
            "htf_bias":   "neutral",
            "h4_bias":    "neutral",
            "atr":        5.0,
            "macd_hist":  0.0,
            "session":    "off",
            "price":      0.0,
        }

        self._price_history: list[tuple[float, float]] = []
        self._velocity_cooldowns: dict[str, float] = {}

        self._closed_count: int = 0
        self._main_engine = None

        self._setup_logger()

    def _setup_logger(self):
        try:
            data_dir = Path(cfg_module.DATA_DIR)
            log_path = data_dir / "breakout_signal.log"
            if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in _log.handlers):
                h = logging.handlers.RotatingFileHandler(
                    str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3
                )
                h.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s — %(message)s"
                ))
                _log.addHandler(h)
                _log.setLevel(logging.INFO)
        except Exception:
            pass

    def add_refresh_callback(self, cb: Callable) -> None:
        self._refresh_cbs.append(cb)

    def _notify_refresh(self):
        for cb in self._refresh_cbs:
            try:
                cb()
            except Exception:
                pass

    def set_main_engine(self, engine) -> None:
        self._main_engine = engine

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.is_running:
            return
        try:
            self._closed_count = bdb.get_stats().get("closed", 0) % 10
        except Exception:
            pass

        try:
            from forex_trader.core import database as _cdb_so
            if _cdb_so.get_channel_strategy_override("Breakout Engine") is None:
                _cdb_so.set_channel_strategy_override("Breakout Engine", "scale_out")
                _log.info(
                    "[BO-Engine] Set live trade strategy override to 'scale_out' "
                    "(was inheriting global default, which discards signal SL/TP)"
                )
        except Exception as _so_exc:
            _log.warning("[BO-Engine] Could not set channel strategy override: %s", _so_exc)
        self.is_running = True
        self.status     = "running"
        self._task_cycle    = asyncio.ensure_future(self._cycle_loop())
        self._task_outcome  = asyncio.ensure_future(self._outcome_loop())
        self._task_velocity = asyncio.ensure_future(self._velocity_loop())
        asyncio.ensure_future(self._reconcile_live_pnl())
        _log.info("[BO-Engine] Started (M5 gate + 3s velocity monitor)")

    def stop(self) -> None:
        self.is_running = False
        self.status     = "stopped"
        for task in (self._task_cycle, self._task_outcome, self._task_velocity):
            if task and not task.done():
                task.cancel()
        _log.info("[BO-Engine] Stopped")

    # ── Main candle-gate loop (every 60s, M5 candles) ────────────────────────

    async def _cycle_loop(self) -> None:
        while self.is_running:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log.exception("[BO-Engine] Cycle error: %s", e)
                self.status_detail = f"Error: {e}"
            await asyncio.sleep(_CYCLE_INTERVAL)

    async def _run_cycle(self) -> None:
        """Full analysis cycle -- runs every 60s. Also refreshes the cached
        state used by the velocity monitor."""
        self.last_cycle_at = time.time()

        from forex_trader.core import database as _db_module
        if await _db_module.to_db_thread(_db_module.is_remote_node):
            self.status_detail = "Remote/VPS node — signal generation is local-node-only"
            return
        if not await _db_module.to_db_thread(_db_module.should_generate_signals_here):
            self.status_detail = "Centralized mode: generation runs on the local node"
            return

        log_entry: dict = {"ts": time.time()}

        try:
            tick        = await self._bridge.get_tick()
            m5_candles  = await self._bridge.get_candles("M5",  80)
            h1_candles  = await self._bridge.get_candles("H1", 120)
            h4_candles  = await self._bridge.get_candles("H4",  40)

            if not tick or not m5_candles or not h1_candles:
                self.status_detail = "No market data"
                log_entry.update({"result": "no_data", "suppressed_reason": "No market data"})
                bdb.log_analysis(log_entry)
                return

            session = get_session()
            log_entry["session"] = session

            if session == "closed":
                log_entry.update({"result": "closed", "suppressed_reason": "Market closed (weekend)"})
                bdb.log_analysis(log_entry)
                return

            _sess_ok, _sess_name = await _db_module.to_db_thread(_db_module.is_session_allowed)
            if not _sess_ok:
                log_entry.update({
                    "result": "session_disabled",
                    "suppressed_reason": f"Session '{_sess_name}' not enabled in Trading Markets",
                })
                bdb.log_analysis(log_entry)
                self.status_detail = f"Session '{_sess_name}' not enabled in Trading Markets"
                return

            current_price = float(tick.ask)
            atr           = compute_atr(m5_candles[-20:], period=14)

            if len(m5_candles) >= 50:
                _atr_ref = compute_atr(m5_candles[-50:-10], period=14)
                if _atr_ref > 0:
                    try:
                        from forex_trader.core import database as _cdb_ac
                        _ac_thr = float(_cdb_ac.get_risk_settings().get("atr_collapse_threshold", 0.65))
                    except Exception:
                        _ac_thr = 0.65
                    if atr / _atr_ref < _ac_thr:
                        reason = (
                            f"ATR collapse: {atr:.1f} is {atr/_atr_ref:.0%} of baseline "
                            f"{_atr_ref:.1f} — dead market suppressed"
                        )
                        log_entry.update({"result": "atr_collapse", "suppressed_reason": reason})
                        bdb.log_analysis(log_entry)
                        self.status_detail = reason
                        return

            htf_bias      = compute_htf_bias(h1_candles)
            h4_bias       = compute_h4_bias(h4_candles) if h4_candles else "neutral"
            m5_closes     = [float(c.get("close", 0) or 0) for c in m5_candles]
            adx           = compute_adx(m5_candles[-30:] if len(m5_candles) >= 30 else m5_candles)
            _, macd_hist  = compute_macd_hist(m5_closes)

            log_entry.update({
                "price":    current_price,
                "atr_m15":  atr,
                "htf_bias": htf_bias,
                "h4_bias":  h4_bias,
                "adx":      round(adx, 1),
            })

            key_levels = identify_key_levels(h1_candles, current_price, m15_candles=m5_candles)

            self._cached.update({
                "key_levels": key_levels[:10],
                "adx":        adx,
                "htf_bias":   htf_bias,
                "h4_bias":    h4_bias,
                "atr":        atr,
                "macd_hist":  macd_hist,
                "session":    session,
                "price":      current_price,
                "m5_candles": m5_candles[-13:],
            })

            log_entry["key_levels"] = key_levels[:8]

            min_adx = min(ap.get("min_adx_go"), ap.get("min_adx_retest"))
            if adx < min_adx:
                log_entry.update({
                    "result": "adx_too_low",
                    "suppressed_reason": (
                        f"ADX {adx:.1f} < {min_adx:.0f} — ranging market, "
                        f"not trending enough for breakout"
                    ),
                })
                bdb.log_analysis(log_entry)
                self.status_detail = f"ADX {adx:.1f} — ranging, velocity monitor active"
                return

            if htf_bias == "neutral" and adx < 40:
                log_entry.update({
                    "result": "no_bias",
                    "suppressed_reason": (
                        f"Neutral H1 bias + ADX {adx:.1f} — no directional conviction"
                    ),
                })
                bdb.log_analysis(log_entry)
                return

            open_sigs = bdb.get_open_signals()
            if len(open_sigs) >= 2:
                log_entry.update({
                    "result": "max_positions",
                    "suppressed_reason": f"Already {len(open_sigs)} open — at position limit",
                })
                bdb.log_analysis(log_entry)
                return

            if is_news_window():
                log_entry.update({
                    "result": "news_window",
                    "suppressed_reason": "News window active",
                })
                bdb.log_analysis(log_entry)
                return

            _spread = float(tick.ask) - float(tick.bid)
            _max_spread = ap.get("max_spread_pts")
            if _spread > _max_spread:
                reason = f"Spread {_spread:.2f} > {_max_spread:.2f} limit — entry cost too high"
                log_entry.update({"result": "spread_gate", "suppressed_reason": reason})
                bdb.log_analysis(log_entry)
                self.status_detail = reason
                return

            context = {
                "adx":       adx,
                "macd_hist": macd_hist,
                "htf_bias":  htf_bias,
                "h4_bias":   h4_bias,
                "session":   session,
                "price":     current_price,
                "atr_m15":   atr,
                "trigger":   "M5_candle",
            }

            candidate = None
            if session != "asian":
                candidate = check_breakout_go(
                    m5_candles, key_levels, htf_bias, h4_bias,
                    current_price, atr, adx, macd_hist,
                )
            if not candidate:
                candidate = check_breakout_retest(
                    m5_candles, key_levels, htf_bias, h4_bias,
                    current_price, atr, adx, macd_hist,
                )
                if candidate:
                    context["trigger"] = "M5_retest"

            if not candidate:
                candidate = check_liquidity_sweep(
                    m5_candles, key_levels, htf_bias, h4_bias,
                    current_price, atr, adx, macd_hist,
                )
                if candidate:
                    context["trigger"] = "M5_sweep"

            if not candidate:
                log_entry.update({
                    "result": "no_trigger",
                    "suppressed_reason": (
                        f"ADX {adx:.1f} | {htf_bias} bias — no M5 level break or retest found"
                    ),
                })
                bdb.log_analysis(log_entry)
                self.status_detail = f"ADX {adx:.1f} | {htf_bias} | scanning M5+velocity"
                return

            log_entry["candidate"] = candidate
            await self._process_candidate(candidate, context, log_entry, atr, adx, tick=tick)

        except Exception as exc:
            _log.exception("[BO-Engine] _run_cycle exception: %s", exc)
            log_entry.update({"result": "error", "suppressed_reason": str(exc)})
            try:
                bdb.log_analysis(log_entry)
            except Exception:
                pass

    # ── Shared candidate processing (M5 gate + velocity both use this) ────────

    async def _process_candidate(
        self,
        candidate: dict,
        context: dict,
        log_entry: dict,
        atr: float,
        adx: float,
        velocity: bool = False,
        tick=None,
    ) -> None:
        """Risk calc → duplicate guard → Claude review → create signal."""
        current_price = context["price"]
        trigger_tag   = "velocity" if velocity else "M5_candle"

        if ap.get("level_filter_enabled") >= 0.5:
            from backend.src.utils.regime import BREAKOUT_BLOCKED_LEVELS
            _blt = candidate.get("broken_level_type", "")
            if _blt in BREAKOUT_BLOCKED_LEVELS:
                _lv_reason = f"Level type '{_blt}' blocked (measured 36-37% WR)"
                if not velocity:
                    log_entry.update({"result": "level_type_blocked", "suppressed_reason": _lv_reason})
                    bdb.log_analysis(log_entry)
                _log.debug("[BO-Engine] %s suppressed: %s", trigger_tag, _lv_reason)
                return

        # Per-level cooldown -- uses the named repo function (030), replacing
        # engine.py's raw `with bdb._conn() as _lc_conn:` bypass.
        try:
            _lc_cutoff = time.time() - _LEVEL_COOLDOWN
            _lc_last = bdb.get_last_signal_time_for_level(
                candidate["direction"], candidate["broken_level"], _lc_cutoff
            )
            if _lc_last:
                _mins_ago = int((time.time() - _lc_last) / 60)
                _reason = (
                    f"{candidate['direction']} level {candidate['broken_level']:.0f} "
                    f"last fired {_mins_ago}min ago — cooldown {_LEVEL_COOLDOWN // 60}min"
                )
                if not velocity:
                    log_entry.update({"result": "level_cooldown", "suppressed_reason": _reason})
                    bdb.log_analysis(log_entry)
                _log.debug("[BO-Engine] %s skipped (%s): %s", trigger_tag, "level_cooldown", _reason)
                return
        except Exception as _lc_exc:
            _log.debug("[BO-Engine] level_cooldown check failed: %s", _lc_exc)

        # Consecutive-loss direction cooldown -- uses the named repo function
        # (030), replacing engine.py's raw `with bdb._conn() as _cl_conn:` bypass.
        try:
            _cl_cutoff = time.time() - _CONSEC_LOSS_WINDOW
            _cl_outcomes = bdb.get_recent_outcomes_by_direction(
                candidate["direction"], _cl_cutoff, _CONSEC_LOSS_LIMIT
            )
            if (
                len(_cl_outcomes) >= _CONSEC_LOSS_LIMIT
                and all(o == "loss" for o in _cl_outcomes)
            ):
                _cl_reason = (
                    f"{_CONSEC_LOSS_LIMIT} consecutive {candidate['direction']} losses "
                    f"in last {_CONSEC_LOSS_WINDOW // 3600}h — direction cooldown"
                )
                if not velocity:
                    log_entry.update({"result": "consec_loss_cooldown", "suppressed_reason": _cl_reason})
                    bdb.log_analysis(log_entry)
                _log.info("[BO-Engine] %s suppressed: %s", trigger_tag, _cl_reason)
                return
        except Exception as _cl_exc:
            _log.debug("[BO-Engine] consec_loss check failed: %s", _cl_exc)

        try:
            from forex_trader.core import database as _cdb
            if _cdb.has_conflict_on_bus("breakout", candidate["direction"], window_seconds=21600.0):
                _conf_reason = (
                    f"Cross-engine conflict: another engine has active "
                    f"{'SELL' if candidate['direction']=='BUY' else 'BUY'} signal"
                )
                if not velocity:
                    log_entry.update({"result": "cross_engine_conflict", "suppressed_reason": _conf_reason})
                    bdb.log_analysis(log_entry)
                _log.info("[BO-Engine] %s suppressed: %s", trigger_tag, _conf_reason)
                return
        except Exception as _cf_exc:
            _log.debug("[BO-Engine] conflict check failed: %s", _cf_exc)

        risk = calculate_breakout_risk_levels(candidate, current_price, atr, adx)
        if not risk:
            log_entry.update({
                "result": "risk_calc_failed",
                "suppressed_reason": "Risk/R:R below minimum — skipped",
            })
            if not velocity:
                bdb.log_analysis(log_entry)
            return

        open_sigs = bdb.get_open_signals()
        same_dir  = [s for s in open_sigs if s.get("direction") == candidate["direction"]]
        if same_dir:
            if not velocity:
                log_entry.update({
                    "result": "duplicate_direction",
                    "suppressed_reason": f"Already have open {candidate['direction']} signal",
                })
                bdb.log_analysis(log_entry)
            return

        try:
            from forex_trader.core.database import get_risk_settings as _grs_claude
            _bo_claude_on = bool(_grs_claude().get("bo_claude_eval_enabled", 1))
        except Exception:
            _bo_claude_on = True

        if _bo_claude_on:
            review = await review_signal(candidate, risk, context)
            _log.info(
                "[BO-Engine] Claude (%s): approved=%s score=%.2f %s",
                trigger_tag, review["approved"], review["score"], review["rationale"][:80],
            )
        else:
            review = {"approved": True, "score": 0.70, "rationale": "Claude eval disabled — ML+rules passed", "fallback": False}
            _log.info("[BO-Engine] Claude eval OFF (%s) — auto-approved", trigger_tag)

        if not review["approved"]:
            ml_info       = bo_ml.summary()
            in_bootstrap  = ml_info.get("labeled_count", 0) < _BOOTSTRAP_SAMPLES
            if in_bootstrap and not review["fallback"]:
                _log.info(
                    "[BO-Engine] Bootstrap override: Claude rejected but only %d/%d samples — "
                    "accepting to build training data",
                    ml_info.get("labeled_count", 0), _BOOTSTRAP_SAMPLES,
                )
                review = dict(review, approved=True, rationale=(
                    f"[bootstrap] {review['rationale']}"
                ))
            else:
                log_entry.update({
                    "result": "claude_rejected",
                    "claude_decision": review["rationale"],
                    "suppressed_reason": (
                        f"Claude rejected ({trigger_tag}): {review['rationale']}"
                        if not review["fallback"]
                        else "Claude error — signal skipped."
                    ),
                    "session":  context.get("session"),
                    "htf_bias": context.get("htf_bias"),
                    "h4_bias":  context.get("h4_bias"),
                    "price":    current_price,
                    "atr_m15":  atr,
                    "adx":      round(adx, 1),
                    "candidate": candidate,
                })
                bdb.log_analysis(log_entry)
                return

        ref_hash   = hashlib.sha256(
            f"{time.time()}{candidate['direction']}{risk['entry_mid']}".encode()
        ).hexdigest()[:8].upper()
        signal_ref = f"BO-{ref_hash}"

        try:
            from forex_trader.core.database import get_risk_settings as _grs
            _bo_strategy = _grs().get("trade_strategy", "conservative")
        except Exception:
            _bo_strategy = "conservative"

        sig_data = {
            "created_at":        time.time(),
            "signal_ref":        signal_ref,
            "direction":         candidate["direction"],
            "breakout_type":     candidate["breakout_type"],
            "broken_level":      candidate["broken_level"],
            "broken_level_type": candidate.get("broken_level_type"),
            "session":           context.get("session"),
            "htf_bias":          context.get("htf_bias"),
            "h4_bias":           context.get("h4_bias"),
            "adx_at_signal":     round(adx, 1),
            "macd_hist":         round(context.get("macd_hist", 0), 4),
            "atr_m15":           round(atr, 2),
            "quality_score":     review["score"],
            "rationale":         review["rationale"],
            "lot_size":          _LOT_SIZE,
            "claude_fallback":   review["fallback"],
            "strategy":          _bo_strategy,
            **risk,
        }
        sig_id = bdb.create_signal(sig_data)

        try:
            from forex_trader.test_signal.market_context import get_context as _get_ctx
            _market_ctx = _get_ctx()
        except Exception:
            _market_ctx = {}

        try:
            from backend.src.utils.news_calendar import get_news_proximity_norm as _get_news
            sig_data["news_proximity_norm"] = _get_news()
        except Exception:
            sig_data["news_proximity_norm"] = 1.0

        try:
            from forex_trader.core import database as _cdb
            sig_data["regime_score"]          = _cdb.get_regime_score(adx, atr)
            sig_data["equity_drawdown_pct"]   = _cdb.get_equity_drawdown_pct()
            sig_data["concurrent_agreement"]  = _cdb.get_concurrent_agreement(
                "breakout", candidate["direction"]
            )
        except Exception:
            sig_data.setdefault("regime_score", 0.5)
            sig_data.setdefault("equity_drawdown_pct", 0.0)
            sig_data.setdefault("concurrent_agreement", 0.0)

        try:
            from forex_trader.core import database as _cdb
            _ml_conf = float(sig_data.get("quality_score") or 0.5)
            _cdb.write_signal_bus("breakout", candidate["direction"], confidence=_ml_conf,
                                   signal_id=sig_id, ttl_seconds=21600.0)
        except Exception:
            pass

        ml_features = bo_ml.extract_features(sig_data, market_ctx=_market_ctx)
        if ml_features and sig_id:
            bdb.store_ml_features(sig_id, ml_features)
            ml_pred = bo_ml.predict(ml_features)
            if ml_pred is not None:
                bdb.store_ml_prob(sig_id, ml_pred)
                log_entry["ml_prob"] = round(ml_pred, 4)
                _log.debug("[BO-Engine] ML predicted R=%.3f", ml_pred)

        log_entry.update({
            "result":         f"signal_created:{signal_ref}",
            "claude_decision": review["rationale"],
            "session":         context.get("session"),
            "htf_bias":        context.get("htf_bias"),
            "h4_bias":         context.get("h4_bias"),
            "price":           current_price,
            "atr_m15":         atr,
            "adx":             round(adx, 1),
            "candidate":       candidate,
        })
        bdb.log_analysis(log_entry)

        _log.info(
            "[BO-Engine] SIGNAL %s (%s): %s %s @ $%.2f | SL $%.2f | TP1 $%.2f | "
            "ADX %.1f | %s",
            signal_ref, trigger_tag,
            candidate["direction"], candidate["breakout_type"],
            risk["entry_mid"], risk["stop_loss"], risk["tp1"], adx,
            review["rationale"][:60],
        )
        self.status_detail = f"{signal_ref} via {trigger_tag}"
        self._notify_refresh()

    # ── Outcome monitoring (every 5s) ─────────────────────────────────────────

    async def _outcome_loop(self) -> None:
        while self.is_running:
            try:
                await self._check_outcomes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log.debug("[BO-Engine] Outcome error: %s", e)
            await asyncio.sleep(_OUTCOME_INTERVAL)

    async def _check_outcomes(self) -> None:
        open_sigs = bdb.get_open_signals()
        if not open_sigs:
            return

        tick = await self._bridge.get_tick()
        if not tick:
            return

        bid = float(tick.bid)
        ask = float(tick.ask)
        now = time.time()
        spread_raw = ask - bid
        cost_pts   = self._compute_cost_pts(spread_raw)

        for sig in open_sigs:
            sig_id    = sig["id"]
            direction = sig["direction"]
            status    = sig.get("status", "pending")
            created   = float(sig.get("created_at", 0) or 0)

            # ── Live-execution closure sync ───────────────────────────────
            if status == "triggered" and sig.get("mt5_ticket") and sig.get("live_exec_status") == "success":
                try:
                    from forex_trader.core import database as _mdb

                    def _fetch_mt5_close():
                        import sqlite3 as _sl3
                        with _sl3.connect(f"file:{_mdb._DB_PATH}?mode=ro", uri=True) as _mc:
                            _mc.row_factory = _sl3.Row
                            return _mc.execute(
                                "SELECT status, mt5_profit, net_pnl, sl_moved_to_be "
                                "FROM vantage_simulated_trades WHERE mt5_ticket=? AND status='closed'",
                                (sig["mt5_ticket"],),
                            ).fetchone()

                    _mrow = await _mdb.to_db_thread(_fetch_mt5_close)
                    if _mrow:
                        _profit = float(_mrow["mt5_profit"] or _mrow["net_pnl"] or 0)
                        _mt5_outcome = (
                            "win"  if _profit > 1.0  else
                            "loss" if _profit < -1.0 else
                            "be"
                        )
                        _eval_close = bid if direction == "BUY" else ask
                        entry = float(sig.get("entry_mid", 0) or 0)
                        lot   = float(sig.get("lot_size") or _LOT_SIZE)
                        _log.info(
                            "[BO-Engine] %s closed via MT5 sync: profit=%.2f → %s",
                            sig.get("signal_ref"), _profit, _mt5_outcome,
                        )
                        self._close_and_learn(
                            sig_id, _eval_close, _mt5_outcome,
                            f"MT5 closed (mt5_profit={_profit:.2f})",
                            entry, direction, lot, cost_pts,
                        )
                        self._notify_refresh()
                        continue
                except Exception as _sync_exc:
                    _log.debug("[BO-Engine] MT5 closure sync failed for %s: %s", sig.get("signal_ref"), _sync_exc)

            if created and (now - created) > _MAX_SIGNAL_AGE:
                bdb.expire_signal(sig_id, f"Expired after {_MAX_SIGNAL_AGE / 3600:.0f}h")
                _log.info("[BO-Engine] %s expired", sig.get("signal_ref"))
                self._notify_refresh()
                continue

            if status == "triggered" and sig.get("mt5_ticket") and sig.get("live_exec_status") == "success":
                continue

            if status == "pending":
                from forex_trader.core.database import is_session_allowed as _isa
                _sess_ok, _sess_name = _isa()
                if not _sess_ok:
                    _log.debug(
                        "[BO-Engine] %s held: session '%s' not enabled in Trading Markets",
                        sig.get("signal_ref"), _sess_name,
                    )
                    continue
                price_exec = ask if direction == "BUY" else bid
                bdb.trigger_signal(sig_id, price_exec)
                _log.info("[BO-Engine] %s triggered @ %.2f", sig.get("signal_ref"), price_exec)
                status = "triggered"
                await self._execute_live(sig, price_exec, tick)

            if status != "triggered":
                continue

            trigger_time = float(sig.get("trigger_time") or 0)
            if (trigger_time > 0
                    and not sig.get("live_exec_status")
                    and (now - trigger_time) > 120):
                bdb.update_live_exec_result(
                    sig_id, None, None, "failed:orphaned_no_response"
                )
                _log.warning(
                    "[BO-Engine] %s triggered %ds ago but live_exec_status never "
                    "written — marking orphaned",
                    sig.get("signal_ref"), int(now - trigger_time),
                )

            # Refresh sig after trigger_signal() above may have changed its state.
            self._manage_triggered_signal(bdb.get_signal_by_id(sig_id) or sig, bid, ask, now, cost_pts)
