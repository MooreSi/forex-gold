"""
GD Copy Engine -- Gold Diggers VIP emulation signal generator.

This is the thin orchestrator (task 040): lifecycle, the signal-generation
cycle, and the outcome-loop's pending/triggered routing live here; the
TP/SL/partial-close ladder (_ManagementMixin), VIP correlation
(_CorrelationMixin), and live order dispatch (_LiveExecuteMixin) are each
their own file. Replaces engine.py -- see
docs/todo/refactor/backend-foundation/040-*.md for what moved where and why.

Strategy:
  1. Identify key S/R levels: Asia range (as level source), swing H/L, round numbers
  2. Generate pending limit-zone signals when price approaches a level
  3. Trigger (activate) when price enters the entry zone
  4. Monitor outcomes; move SL to BE after TP1; learn from results
  5. Continuously correlate our signals with actual GD VIP signals to
     benchmark accuracy -- goal is to fire BEFORE GD VIP (negative lead time)

Session: 04:00-16:00 UTC -- measured from 591 real GD VIP signals: 23% arrive
  04:00-06:59 (late-Asia push on the Asia range), 72% 07:00-14:59, 5% after
  15:00. Asia range is used as a level source (not a trading session).
Signal expiry: 2 hours (matches bounce engine)

Lead time (correlation_time_delta_s) is SIGNED:
  negative = we fired first (good -- we predicted the level)
  positive = GD VIP fired first (we lagged)
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import forex_trader.config as _cfg_module
from forex_trader.gd_copy_signal import gd_copy_signal_repo as gdc_db
from forex_trader.gd_copy_signal import level_detector as ld
from forex_trader.gd_copy_signal import ml_engine as gdc_ml
from forex_trader.gd_copy_signal import signal_generator as sg
from forex_trader.gd_copy_signal.gd_copy_signal_correlate import _CorrelationMixin
from forex_trader.gd_copy_signal.gd_copy_signal_live_execute import _LiveExecuteMixin
from forex_trader.gd_copy_signal.gd_copy_signal_manage import _ManagementMixin

_log = logging.getLogger("gd_copy_signal")


def _setup_logger() -> None:
    """Attach a rotating file handler to the gd_copy_signal logger."""
    try:
        data_dir = Path(_cfg_module.DATA_DIR)
        log_path = data_dir / "gd_copy_signal.log"
        if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in _log.handlers):
            h = logging.handlers.RotatingFileHandler(
                str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3
            )
            h.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s — %(message)s"
            ))
            _log.addHandler(h)
            _log.setLevel(logging.DEBUG)
    except Exception:
        pass

# ── Constants ──────────────────────────────────────────────────────────────────
_CYCLE_INTERVAL_S     = 60      # main cycle: 1 minute
_OUTCOME_INTERVAL_S   = 5       # outcome/trigger loop
_CORR_INTERVAL_S      = 30      # correlation check loop
_SIGNAL_MAX_AGE_S     = 7200    # 2-hour pending expiry
_LEVEL_COOLDOWN_S     = 1800    # 30 min before same level re-signals
_MAX_OPEN_SIGNALS     = 3       # cap concurrent open positions
_CONSEC_LOSS_LIMIT    = 3       # consecutive same-direction losses -> cooldown
_CONSEC_LOSS_WINDOW   = 7200    # 2-hour window for consecutive-loss check

# How many gate-passing candidates the ML model gets a vote on each cycle,
# before signal creation (previously the model's prediction was only ever
# computed AFTER a signal was already created and committed to -- it could
# block a live-money fill via _try_live_execute's re-check, but had zero
# say in which of several qualifying levels became the signal in the first
# place). Bounded to the top-3 by level_detector's own score rather than
# the full candidate list, so a noisy prediction on a much weaker level
# can't win outright -- ML narrows the choice among already-good levels,
# it doesn't override level quality entirely.
_ML_CANDIDATE_POOL = 3

def rank_eligible_candidates(
    eligible: list[tuple[dict, str, dict, Optional[list[float]], Optional[float]]],
) -> list[tuple[dict, str, dict, Optional[list[float]], Optional[float]]]:
    """Re-rank (level, direction, sig_data, feats, prob) tuples by predicted
    R-multiple when the model has an opinion on more than one candidate;
    falls back to the original level_detector-score ordering (unchanged)
    when no model is trained yet, so a cold-start engine behaves exactly as
    it did before ML got a vote in signal creation. Module-level and pure
    so it's testable without the rest of _run_cycle's bridge/DB coupling."""
    if any(t[4] is not None for t in eligible):
        return sorted(eligible, key=lambda t: t[4] if t[4] is not None else float("-inf"), reverse=True)
    return eligible


# ── Singleton ─────────────────────────────────────────────────────────────────
_instance: Optional["GDCopyEngine"] = None


def get_instance() -> Optional["GDCopyEngine"]:
    return _instance


def init(bridge) -> "GDCopyEngine":
    global _instance
    if _instance is None:
        _setup_logger()
        _instance = GDCopyEngine(bridge)
    return _instance


class GDCopyEngine(_ManagementMixin, _CorrelationMixin, _LiveExecuteMixin):
    def __init__(self, bridge):
        self._bridge    = bridge
        self._main_eng  = None
        self.is_running = False
        self._tasks:    list[asyncio.Task] = []
        self._refresh_cbs: list[Callable] = []
        self._last_cycle_ts: Optional[float] = None
        self._status_msg = "Stopped"

        # Cached market state from last cycle
        self._cached: dict = {
            "price":    0.0,
            "atr":      8.0,
            "adx":      0.0,
            "htf_bias": "neutral",
            "h1_bias":  "neutral",
            "session":  "off",
            "levels":   [],
        }

        # Debounce for _reconcile_live_signal -- a ticket missing from
        # get_positions() can be a transient bridge hiccup, not a real close.
        self._live_missing_streak: dict[int, int] = {}

    def set_main_engine(self, engine) -> None:
        self._main_eng = engine

    def add_refresh_callback(self, cb: Callable) -> None:
        self._refresh_cbs.append(cb)

    def _notify_refresh(self) -> None:
        for cb in self._refresh_cbs:
            try:
                cb()
            except Exception:
                pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._status_msg = "Running"
        gdc_db.set_config("gdc_user_stopped", "0")
        loop = asyncio.get_event_loop()
        self._tasks = [
            loop.create_task(self._cycle_loop()),
            loop.create_task(self._outcome_loop()),
            loop.create_task(self._correlation_loop()),
        ]
        _log.info("[GDC-Engine] started")

    def stop(self, persist: bool = True) -> None:
        self.is_running = False
        self._status_msg = "Stopped"
        if persist:
            # Only write when the user explicitly stops -- app shutdown calls
            # stop(persist=False) so the preference survives across restarts.
            gdc_db.set_config("gdc_user_stopped", "1")
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        _log.info("[GDC-Engine] stopped (persist=%s)", persist)

    # ── Main cycle ────────────────────────────────────────────────────────────

    async def _cycle_loop(self) -> None:
        """60-second main loop: detect levels, generate signals."""
        while self.is_running:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.warning("[GDC-Engine] cycle error: %s", exc)
            await asyncio.sleep(_CYCLE_INTERVAL_S)

    async def _run_cycle(self) -> None:
        self._last_cycle_ts = time.time()

        # Centralized signal generation (Settings > Remote Node): once this
        # VPS is the active trader and generation has moved to the Mac, skip
        # the whole analysis cycle rather than running it and having its
        # eventual open_trade() call just get forwarded/rejected -- this is
        # what actually saves the CPU on the VPS.
        from forex_trader.core import database as _db_module
        if await _db_module.to_db_thread(_db_module.is_remote_node):
            self._status_msg = "Remote/VPS node — signal generation is local-node-only"
            return
        if not await _db_module.to_db_thread(_db_module.should_generate_signals_here):
            self._status_msg = "Centralized mode: generation runs on the local node"
            return

        now_utc = datetime.now(timezone.utc)
        utc_hour = now_utc.hour

        # Session gate -- was a hardcoded 00:00-16:00 UTC window (measured
        # GD VIP posting hours), which fought the user-facing
        # Trading Markets toggles (Settings > Strategy): a session the user
        # explicitly enabled could still be silently skipped, and a session
        # they'd disabled could still generate outside that window. Now uses
        # the same is_session_allowed() gate every other engine's real
        # execution respects, so Asia/London/NY on/off is the single source
        # of truth for whether this engine analyses at all.
        _sess_ok, _sess_name = await _db_module.to_db_thread(_db_module.is_session_allowed)
        if not _sess_ok:
            self._status_msg = f"Session '{_sess_name}' not enabled in Trading Markets"
            gdc_db.log_analysis({
                "result": "session_closed",
                "reason": f"Session '{_sess_name}' (UTC {utc_hour:02d}:xx) not enabled in Trading Markets",
            })
            return

        # Fetch candles
        try:
            h1_candles  = await self._bridge.get_candles("H1", 50)
            # 80 bars (~20h) rather than the old 20 -- the GD2/Unicorn
            # detector (ict_patterns.find_unicorn_setup) needs real lookback
            # for its structure-shift and breaker-block checks, validated
            # against live history at 60+ bars.
            m15_candles = await self._bridge.get_candles("M15", 80)
            # get_htf_bias()'s H4-confirmation branch existed but was never
            # given h4_candles at either call site, so H1 alone decided bias
            # for every signal, the live-execution bias re-check, and the
            # htf_bias_score/bias_aligned ML features. 6 bars (~24h) is all
            # get_htf_bias() actually looks at (h4_candles[-6:]).
            h4_candles  = await self._bridge.get_candles("H4", 6)
            tick        = await self._bridge.get_tick()
        except Exception as exc:
            _log.debug("[GDC-Engine] candle fetch error: %s", exc)
            self._status_msg = "MT5 bridge offline"
            return

        if not tick or not h1_candles:
            self._status_msg = "No price data"
            return

        price = float(tick.mid or tick.bid or 0)
        if price <= 0:
            return

        # Market context
        atr     = self._calc_atr(m15_candles or h1_candles)
        adx     = self._calc_adx(h1_candles)
        htf     = ld.get_htf_bias(h1_candles, h4_candles)
        session = ld.get_session(utc_hour)

        self._cached.update({
            "price":    price,
            "atr":      atr,
            "adx":      adx,
            "htf_bias": htf,
            "session":  session,
        })

        # ATR gate -- don't signal in dead/flash-crash markets
        if atr < 2.0 or atr > 80.0:
            self._status_msg = f"ATR gate: {atr:.1f}"
            return

        # Open position cap
        open_sigs = gdc_db.get_open_signals()
        if len(open_sigs) >= _MAX_OPEN_SIGNALS:
            self._status_msg = f"Cap: {len(open_sigs)} open signals"
            return

        # Get candidate levels
        candidates = ld.get_candidate_levels(h1_candles, price, htf_bias=htf, m15_candles=m15_candles)
        self._cached["levels"] = candidates
        # Full, unfiltered level set (asia/swing/round/congestion, no proximity
        # or score cutoff) -- used by _classify_vip_level() for VIP-level
        # pattern learning, which needs to classify prices the bot itself
        # never got close enough to act on this cycle.
        self._cached["all_levels"] = ld.get_all_levels(h1_candles, price)

        if not candidates:
            self._status_msg = "No candidate levels near price"
            gdc_db.log_analysis({
                "ts": time.time(), "session": session, "htf_bias": htf,
                "price": price, "atr": atr, "adx": adx,
                "result": "no_levels",
            })
            return

        context = {
            "atr":            atr,
            "adx":            adx,
            "htf_bias":       htf,
            "h1_bias":        htf,  # use H1 as proxy
            "session":        session,
            "price_at_signal": price,
        }

        # Deactivate stale levels
        gdc_db.deactivate_old_levels()

        # Touch-track every candidate level type seen this cycle (matched or not) --
        # builds the true denominator for vip_match_rate_for_type.
        for level in candidates:
            gdc_ml.record_level_touch(level["type"])

        cadence = await self._vip_cadence_stats()

        # Context that's the same regardless of which candidate ends up
        # chosen -- computed once per cycle rather than repeated (or, as
        # before, computed only after a candidate had already been picked).
        try:
            from forex_trader.core.news_calendar import get_news_proximity_norm as _get_news
            news_proximity_norm = _get_news()
        except Exception:
            news_proximity_norm = 1.0
        try:
            from forex_trader.core import database as _cdb_feat
            regime_score         = _cdb_feat.get_regime_score(context.get("adx", 0), context.get("atr", 5))
            equity_drawdown_pct  = _cdb_feat.get_equity_drawdown_pct()
        except Exception:
            regime_score        = 0.5
            equity_drawdown_pct = 0.0
        try:
            vip_discipline_score, vip_aggression_score = gdc_ml.get_daily_research_scores()
        except Exception:
            vip_discipline_score, vip_aggression_score = 0.5, 0.5
        win_rate = gdc_db.get_recent_win_rate(20)

        # Gather up to _ML_CANDIDATE_POOL gate-passing candidates (built +
        # feature-extracted, not yet persisted) so the ML model can weigh in
        # on which one becomes the signal -- previously ml_prob was only
        # ever computed AFTER a signal had already been created for
        # whichever level cleared level_detector's own score first.
        eligible: list[tuple[dict, str, dict, Optional[list[float]], Optional[float]]] = []
        for level in candidates:
            if len(eligible) >= _ML_CANDIDATE_POOL:
                break
            if level["score"] < 0.50:
                continue
            direction = level["direction"]

            # Bias filter: strong bias override -- don't fight it
            if htf == "bullish" and direction == "SELL" and level["score"] < 0.75:
                continue
            if htf == "bearish" and direction == "BUY" and level["score"] < 0.75:
                continue

            # Per-level cooldown
            if self._level_on_cooldown(level["price"], direction):
                continue

            # Don't duplicate same direction+level already open
            if self._already_open(direction, level["price"]):
                continue

            # Consecutive-loss direction cooldown
            try:
                _cl_cutoff = time.time() - _CONSEC_LOSS_WINDOW
                _cl_outcomes = gdc_db.get_recent_outcomes_by_direction(
                    direction, _cl_cutoff, _CONSEC_LOSS_LIMIT
                )
                if len(_cl_outcomes) >= _CONSEC_LOSS_LIMIT and all(o == "loss" for o in _cl_outcomes):
                    _log.info("[GDC-Engine] %s cooldown: %d consec losses in 2h", direction, _CONSEC_LOSS_LIMIT)
                    continue
            except Exception:
                pass

            # Cross-engine conflict suppression
            try:
                from forex_trader.core import database as _cdb_cf
                if _cdb_cf.has_conflict_on_bus("gd_copy", direction, window_seconds=180.0):
                    _log.info("[GDC-Engine] %s suppressed — cross-engine conflict", direction)
                    continue
            except Exception:
                pass

            sig_data = sg.build_signal(level, direction, context)
            sig_data["strategy"] = self._active_strategy()
            sig_data["news_proximity_norm"]    = news_proximity_norm
            sig_data["regime_score"]           = regime_score
            sig_data["equity_drawdown_pct"]    = equity_drawdown_pct
            sig_data["vip_discipline_score"]   = vip_discipline_score
            sig_data["vip_aggression_score"]   = vip_aggression_score
            sig_data["minutes_since_last_vip"] = cadence[0]
            sig_data["vip_signals_today"]      = cadence[1]
            try:
                from forex_trader.core import database as _cdb_agree
                sig_data["concurrent_agreement"] = _cdb_agree.get_concurrent_agreement("gd_copy", direction)
            except Exception:
                sig_data["concurrent_agreement"] = 0.0

            feats = gdc_ml.extract_features(sig_data, win_rate)
            prob  = gdc_ml.predict(feats) if feats else None
            eligible.append((level, direction, sig_data, feats, prob))

        signal_created = False
        if eligible:
            level, direction, sig_data, feats, prob = rank_eligible_candidates(eligible)[0]

            sig_id = gdc_db.create_signal(sig_data)
            if sig_id:
                # Write to shared signal bus
                try:
                    from forex_trader.core import database as _cdb_bus
                    _cdb_bus.write_signal_bus(
                        "gd_copy", direction,
                        confidence=float(level.get("score", 0.5)),
                        signal_id=sig_id,
                    )
                except Exception:
                    pass

                if feats:
                    gdc_db.store_ml_features(sig_id, feats)
                    if prob is not None:
                        gdc_db.store_ml_prob(sig_id, prob)

                # Track level
                gdc_db.upsert_level(
                    level["type"], level["price"], direction,
                    source="engine",
                    notes=f"score={level['score']:.2f} atr={atr:.1f}"
                )

                # Update daily correlation tally (signal already in DB, no +1)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                gdc_db.upsert_daily_correlation(today, gdc_signals_sent=gdc_db.count_today_signals())

                _log.info(
                    "[GDC-Engine] SIGNAL %s %s %s level=%.2f score=%.2f ml_prob=%s (%d candidate(s) considered)",
                    sig_data["signal_ref"], direction, level["type"],
                    level["price"], level["score"],
                    f"{prob:.2f}" if prob is not None else "?", len(eligible),
                )

                signal_created = True
                self._notify_refresh()

        if not signal_created:
            top = candidates[0] if candidates else {}
            self._status_msg = (
                f"Top level {top.get('price', 0):.0f} "
                f"({top.get('type', '?')}, score={top.get('score', 0):.2f}) — "
                f"no signal this cycle"
            )
        else:
            self._status_msg = "Signal created"

        gdc_db.log_analysis({
            "ts": time.time(), "session": session, "htf_bias": htf,
            "price": price, "atr": atr, "adx": adx,
            "levels": [{"p": l["price"], "t": l["type"], "s": l["score"]}
                       for l in candidates[:4]],
            "result": "signal" if signal_created else "no_signal",
        })

    # ── Outcome loop ──────────────────────────────────────────────────────────

    async def _outcome_loop(self) -> None:
        """5-second loop: monitor active signals, move SL, close on TP/SL, expire old."""
        while self.is_running:
            try:
                await self._check_outcomes()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.debug("[GDC-Engine] outcome error: %s", exc)
            await asyncio.sleep(_OUTCOME_INTERVAL_S)

    async def _check_outcomes(self) -> None:
        try:
            tick = await self._bridge.get_tick()
            if not tick:
                return
            price = float(tick.mid or tick.bid or 0)
            if price <= 0:
                return
        except Exception:
            return

        open_sigs = gdc_db.get_open_signals()
        now = time.time()

        for sig in open_sigs:
            sig_id    = sig["id"]
            status    = sig["status"]
            direction = sig["direction"]
            entry_lo  = sig["entry_low"]
            entry_hi  = sig["entry_high"]
            created   = float(sig.get("created_at", 0))

            # Expire old pending signals
            if status == "pending" and (now - created) > _SIGNAL_MAX_AGE_S:
                gdc_db.expire_signal(sig_id, "max_age_exceeded")
                _log.info("[GDC-Engine] signal %s expired (>2h pending)", sig["signal_ref"])
                self._notify_refresh()
                continue

            # Pending -> trigger when price enters zone
            if status == "pending":
                if entry_lo <= price <= entry_hi:
                    fill = self._realistic_fill(tick, direction, closing=False)
                    gdc_db.trigger_signal(sig_id, fill)
                    _log.info(
                        "[GDC-Engine] TRIGGERED %s @ %.2f (fill, mid=%.2f)",
                        sig.get("signal_ref", sig_id), fill, price
                    )
                    # Optionally execute live trade
                    await self._try_live_execute(sig, fill, tick)
                    self._notify_refresh()
                continue

            # Triggered: monitor SL / TP
            if status != "triggered":
                continue

            # Orphan watchdog: live execution was on at trigger time but
            # live_exec_status was never written (crash/restart mid-execute).
            trigger_time = float(sig.get("trigger_time") or 0)
            if (trigger_time > 0
                    and not sig.get("live_exec_status")
                    and (now - trigger_time) > 120):
                gdc_db.update_live_exec(
                    sig_id, status="failed:orphaned_no_response"
                )
                _log.warning(
                    "[GDC-Engine] signal %s triggered %ds ago but live_exec_status "
                    "never written — marking orphaned",
                    sig.get("signal_ref", sig_id), int(now - trigger_time),
                )

            # A real MT5 trade exists for this signal -- its outcome/P&L is
            # driven entirely by that trade's actual result, never by the
            # tick-based simulation. The real trade is managed independently
            # by the main engine's own strategy (SL moves, partials,
            # trailing), which routinely diverges completely from the
            # simulated ladder in _manage_triggered_signal -- confirmed
            # live: simulated P&L bore no relation to real P&L, and in
            # several cases even had the opposite sign. See
            # _reconcile_live_signal (gd_copy_signal_manage.py).
            if sig.get("mt5_ticket") and sig.get("live_exec_status") == "executed":
                await self._reconcile_live_signal(sig)
                continue

            await self._manage_triggered_signal(sig, tick)

    # ── Correlation loop ──────────────────────────────────────────────────────

    async def _correlation_loop(self) -> None:
        """30-second loop: match our signals against actual GD VIP signals.
        Lead time is SIGNED: negative = we fired first (goal), positive = we lagged.
        """
        while self.is_running:
            try:
                await self._check_correlation()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.debug("[GDC-Engine] correlation error: %s", exc)
            await asyncio.sleep(_CORR_INTERVAL_S)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _active_strategy(self) -> str:
        """The strategy actually selected for the GD VIP channel -- same one used to
        manage real GD VIP trades -- so GDC's virtual outcomes are modelled on the
        same SL/TP management rules, not an unrelated leftover default."""
        try:
            from forex_trader.core import database as core_db
            from forex_trader.core.models import STRATEGY_SCALE_OUT
            override = core_db.get_channel_strategy_override("GD Copy Engine")
            if override and override != "auto":
                return override
            rs = core_db.get_risk_settings()
            return rs.get("trade_strategy", STRATEGY_SCALE_OUT)
        except Exception:
            return "scale_out"

    def _level_on_cooldown(self, price: float, direction: str) -> bool:
        """True if a signal for this level+direction was created within the cooldown window."""
        cutoff = time.time() - _LEVEL_COOLDOWN_S
        recent = gdc_db.get_all_signals(limit=20)
        for s in recent:
            if (s.get("direction") == direction
                    and abs(float(s.get("level_price", 0) or 0) - price) <= 3.0
                    and float(s.get("created_at", 0)) > cutoff):
                return True
        return False

    def _already_open(self, direction: str, level_price: float) -> bool:
        """True if there's already an open signal for this direction+level."""
        for s in gdc_db.get_open_signals():
            if (s.get("direction") == direction
                    and abs(float(s.get("level_price", 0) or 0) - level_price) <= 3.0):
                return True
        return False

    def _today_signal_count(self) -> int:
        """Count of signals created today (UTC). Uses SQL COUNT -- not capped by limit."""
        return gdc_db.count_today_signals()

    @staticmethod
    def _calc_atr(candles: list[dict], period: int = 14) -> float:
        """Simple ATR from candle high/low."""
        if not candles or len(candles) < 2:
            return 8.0
        trs = []
        for i in range(1, min(period + 1, len(candles))):
            h = float(candles[i].get("high", candles[i].get("h", 0)) or 0)
            l = float(candles[i].get("low",  candles[i].get("l", 0)) or 0)
            pc = float(candles[i-1].get("close", candles[i-1].get("c", 0)) or 0)
            if h > 0 and l > 0:
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return round(sum(trs) / len(trs), 2) if trs else 8.0

    @staticmethod
    def _calc_adx(candles: list[dict], period: int = 14) -> float:
        """Simplified ADX (uses ATR-normalised DM average as proxy)."""
        if not candles or len(candles) < period + 1:
            return 25.0
        plus_dms, minus_dms, trs = [], [], []
        for i in range(1, len(candles)):
            h  = float(candles[i].get("high",  candles[i].get("h", 0)) or 0)
            l  = float(candles[i].get("low",   candles[i].get("l", 0)) or 0)
            ph = float(candles[i-1].get("high", candles[i-1].get("h", 0)) or 0)
            pl = float(candles[i-1].get("low",  candles[i-1].get("l", 0)) or 0)
            pc = float(candles[i-1].get("close",candles[i-1].get("c", 0)) or 0)
            if h <= 0 or l <= 0:
                continue
            up_move   = h - ph
            down_move = pl - l
            plus_dm   = up_move   if up_move > down_move and up_move > 0   else 0
            minus_dm  = down_move if down_move > up_move and down_move > 0 else 0
            tr = max(h - l, abs(h - pc), abs(l - pc)) if pc > 0 else (h - l)
            plus_dms.append(plus_dm)
            minus_dms.append(minus_dm)
            trs.append(tr)

        if not trs:
            return 25.0

        atr14  = sum(trs[-period:]) / period
        if atr14 <= 0:
            return 25.0
        plus14  = sum(plus_dms[-period:])  / period
        minus14 = sum(minus_dms[-period:]) / period
        di_plus  = plus14  / atr14 * 100
        di_minus = minus14 / atr14 * 100
        di_sum   = di_plus + di_minus
        if di_sum == 0:
            return 25.0
        dx = abs(di_plus - di_minus) / di_sum * 100
        return round(dx, 1)

    def get_status(self) -> dict:
        return {
            "is_running":     self.is_running,
            "status_msg":     self._status_msg,
            "last_cycle_ts":  self._last_cycle_ts,
            "cached":         self._cached,
        }
