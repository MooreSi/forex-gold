"""Live (demo/real) MT5 order dispatch for a triggered Reversal Engine signal --
extracted verbatim (no logic changes) from engine.py's _try_live_execute as
part of task 040. See docs/todo/refactor/backend-foundation/040-*.md.

Kept as its own file per backend-conventions' decomposition pattern --
"the writes: dispatch/submit" get their own file, separate from the
management/correlation concerns. _LiveExecuteMixin is composed into
ReversalEngine (reversal_engine_service.py).

Real-money surface: this is the one path in reversal_engine that can place
an actual MT5 order. Task 050 (demo-account validation) is the only task
in this pack allowed to exercise it against a live connection.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from forex_trader.core import database as db_module
from forex_trader.core.momentum_exhaustion import check_momentum_exhaustion
from forex_trader.reversal_engine import reversal_engine_repo as re_db
from forex_trader.reversal_engine import level_detector as ld

_log = logging.getLogger("reversal_engine")

_ML_BLOCK_THRESHOLD = 0.0     # block live execution if predicted R-multiple < 0


class _LiveExecuteMixin:
    async def _try_live_execute(self, sig: dict, trigger_price: float, tick=None) -> None:
        """Execute a real MT5 trade if live execution is enabled and ML gate passes."""
        try:
            from forex_trader.core import database as core_db
            rs = core_db.get_risk_settings()
            if not rs.get("re_live_execution", 0):
                # Write a status so the orphan watchdog doesn't misfire on healthy
                # virtual signals when live execution is deliberately disabled.
                re_db.update_live_exec(sig["id"], status="skipped:live_disabled")
                return

            # Trading Schedule gate, Reversal Engine source (2026-07-24) --
            # this engine performs well overnight (Asia) but loses during
            # London/NY, the opposite of the Telegram channels, so each
            # window independently allows/blocks it rather than one blanket
            # automated-order switch. See core_trading_schedule.py.
            from forex_trader.core.core_trading_schedule import check_trading_schedule
            _sched_ok, _sched_reason = check_trading_schedule(source="reversal_engine")
            if not _sched_ok:
                re_db.update_live_exec(sig["id"], status="skipped:schedule")
                _log.info("[RE-Engine] schedule blocked live exec %s -- %s",
                          sig.get("signal_ref"), _sched_reason)
                return

            # Internal Engine Exposure guard (Trading > Strategy) -- OFF by
            # default, in which case this is a no-op. See
            # core_internal_exposure_guard.py for the modes and for the
            # measured reason the default is off.
            from forex_trader.core.core_internal_exposure_guard import check_internal_exposure
            _exp_lot = float(rs.get("strategy_lot_size", 0) or 0) or 0.01
            _exp_ok, _exp_reason = check_internal_exposure(
                sig.get("direction", ""), _exp_lot, rs,
            )
            if not _exp_ok:
                re_db.update_live_exec(sig["id"], status="skipped:exposure_guard")
                _log.info("[RE-Engine] exposure guard blocked live exec %s -- %s",
                          sig.get("signal_ref"), _exp_reason)
                return

            # Fill-time re-evaluation (2026-07-17) -- a pending zone signal can
            # sit anywhere from a couple of minutes to 4h (Reversal Runner/
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
                        re_db.store_ml_prob_at_fill(sig["id"], fresh_prob or 0.0, fresh_htf)
                        re_db.update_live_exec(sig["id"], status="bias_skipped")
                        _log.info(
                            "[RE-Engine] bias gate blocked live exec %s -- htf now %s vs "
                            "direction=%s, level_score=%.2f < 0.75",
                            sig.get("signal_ref"), fresh_htf, direction, level_score,
                        )
                        return

                    # Momentum-exhaustion / rejection re-check (2026-07-28) --
                    # the bias re-check above only asks "does the wider H1/H4
                    # trend still agree", which says nothing about whether the
                    # market has already made (or reversed) its move on the
                    # timeframe this signal actually trades. Deliberately a
                    # fast local check, not an ML/AI call -- see
                    # core/momentum_exhaustion.py's own docstring for why.
                    _mx_ok, _mx_reason = check_momentum_exhaustion(
                        direction, m15_candles or h1_candles, fresh_atr)
                    if not _mx_ok:
                        re_db.store_ml_prob_at_fill(sig["id"], fresh_prob or 0.0, fresh_htf)
                        re_db.update_live_exec(sig["id"], status="momentum_skipped")
                        _log.info(
                            "[RE-Engine] momentum re-check blocked live exec %s -- %s",
                            sig.get("signal_ref"), _mx_reason,
                        )
                        return

                    # Fresh ML re-score -- same feature set ml_engine.
                    # extract_features used at creation, with every dynamic
                    # input (bias/ATR/ADX/session/news/regime/drawdown/
                    # agreement/REF cadence) recomputed against now instead
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
                            "reversal_engine", direction)
                    except Exception:
                        pass
                    try:
                        from forex_trader.reversal_engine import ml_engine as re_ml
                        fresh_sig["ref_discipline_score"], fresh_sig["ref_aggression_score"] = \
                            re_ml.get_daily_research_scores()
                    except Exception:
                        pass
                    cadence = await self._ref_cadence_stats()
                    fresh_sig["minutes_since_last_ref"] = cadence[0]
                    fresh_sig["ref_signals_today"]      = cadence[1]

                    from forex_trader.reversal_engine import ml_engine as re_ml
                    win_rate = re_db.get_recent_win_rate(20)
                    fresh_feats = re_ml.extract_features(fresh_sig, win_rate)
                    if fresh_feats:
                        _fp = re_ml.predict(fresh_feats)
                        if _fp is not None:
                            fresh_prob = _fp
                    re_db.store_ml_prob_at_fill(sig["id"], fresh_prob or 0.0, fresh_htf)
            except Exception as _refresh_exc:
                _log.warning(
                    "[RE-Engine] fill-time re-evaluation failed for %s, falling back to "
                    "creation-time ml_prob: %s", sig.get("signal_ref"), _refresh_exc,
                )

            # ML gate: block live execution when predicted R-multiple < 0 (expected loss)
            if fresh_prob is not None and float(fresh_prob) < _ML_BLOCK_THRESHOLD:
                re_db.update_live_exec(sig["id"], status="ml_skipped")
                _log.info("[RE-Engine] ML gate blocked live exec (predicted_R=%.3f)", float(fresh_prob))
                return

            if self._main_eng is None:
                return

            # Persist to vantage_signals first -- open_trade_from_signal looks up
            # the signal by signal_id, it does not accept a Signal object.
            # Strategy is resolved automatically downstream via the channel
            # override lookup for source_name="Reversal Engine" (see
            # _active_strategy, which mirrors the same lookup).
            main_sig = self._main_eng.create_signal(
                source_name = "Reversal Engine",
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
                notes       = f"RE {sig.get('signal_ref', '')} level={sig.get('level_type', '')}",
            )
            vantage_sig_id = main_sig["signal_id"]

            # LIMIT ORDER toggle (Reversal Engine page, "Active Positions"
            # card, 2026-07-23) -- routes via a genuine EA pending order
            # instead of the market-fill flow below. Unlike Limit Runner/ORB
            # (built with no fallback, since those are opt-in new features),
            # this toggle is meant as a drop-in swap for existing behavior:
            # if the EA isn't reachable, falls through to the normal
            # open_trade_from_signal() market-fill path below rather than
            # skipping the signal outright -- "acts as it does now" is the
            # explicit off-state and the honest degraded-on-state both.
            if rs.get("re_use_limit_order", 0):
                _placed = await self._try_re_limit_order(sig, vantage_sig_id, tick)
                if _placed is not None:
                    return

            trade = await self._main_eng.open_trade_from_signal(vantage_sig_id, tick=tick)
            if trade:
                re_db.update_live_exec(
                    sig["id"],
                    mt5_ticket=trade.get("mt5_ticket"),
                    vantage_sig_id=vantage_sig_id,
                    status="executed",
                )
                _log.info("[RE-Engine] live trade opened mt5=%s", trade.get("mt5_ticket", "?"))
            else:
                re_db.update_live_exec(sig["id"], status="open_failed")
        except Exception as exc:
            _log.warning("[RE-Engine] live exec error: %s", exc)
            try:
                re_db.update_live_exec(sig["id"], status=f"error:{exc}")
            except Exception:
                pass

    async def _try_re_limit_order(self, sig: dict, vantage_sig_id: str, tick) -> Optional[bool]:
        """Places a genuine EA pending limit order for a Reversal Engine signal
        instead of the market-fill flow. Returns True once handled
        (whatever the outcome -- placed, rejected, or a duplicate-claim
        no-op), or None if the EA isn't reachable at all, telling the
        caller to fall back to the normal market-fill path.

        Strategy/lot-size/SL are resolved the exact same way
        open_trade_from_signal() resolves them (same channel-override
        lookup, same session/schedule/circuit-breaker gates) via
        resolve_open_trade_params -- this function only diverges at the
        final "how is the order actually placed" step. Ladder pcts/be_at_pos/
        trail_mode are looked up from core_open_trade's own tables so an
        EA-managed fill of the resolved strategy behaves identically whether
        it arrived via a market fill or a pending-order fill; a non-ladder
        strategy (e.g. Conservative) gets harmless placeholder values since
        its own EA management branch (ManageConservativeLike, etc.) never
        reads t.pcts/t.beAtPos at all.
        """
        from forex_trader.core import ea_bridge as _ea_mod
        _ea = _ea_mod.get_instance()
        if _ea is None or not _ea.is_ea_healthy():
            return None

        from forex_trader.core.core_open_trade import (
            _EA_LADDER_PCTS, _EA_LADDER_BE_AT_POS, _EA_LADDER_TRAIL_MODE,
        )
        from forex_trader.core.core_signal_resolution import resolve_open_trade_params

        try:
            resolved = await resolve_open_trade_params(self._bridge, vantage_sig_id, tick=tick)
        except Exception as exc:
            _log.info("[RE-Engine] limit-order resolve failed (%s) -- gate/claim skip", exc)
            re_db.update_live_exec(sig["id"], status=f"limit_order_skip:{exc}")
            return True

        strategy  = resolved["strategy"]
        lot_size  = resolved["lot_size"]
        stop_loss = resolved["stop_loss_to_use"]
        sig_row   = resolved["sig"]
        direction = sig_row["direction"].upper()
        entry_low  = float(sig_row["entry_low"])
        entry_high = float(sig_row["entry_high"])
        # Near edge of the signal's own entry zone -- same convention every
        # other genuine-pending-order caller uses (Limit Runner, ORB/IVB).
        price = entry_high if direction == "BUY" else entry_low

        tps = {n: float(sig_row[f"tp{n}"]) for n in range(1, 9) if sig_row.get(f"tp{n}") is not None}
        if strategy in _EA_LADDER_PCTS and tps:
            _table = _EA_LADDER_PCTS[strategy]
            pcts = _table.get(len(tps), _table[max(_table)])
            be_at_pos = _EA_LADDER_BE_AT_POS[strategy]
            trail_mode = _EA_LADDER_TRAIL_MODE.get(strategy)
        else:
            pcts, be_at_pos, trail_mode = [1.0], 0, None

        # Atomic claim -- same pattern core_open_trade_from_signal.
        # open_trade_from_signal uses, so a signal can't be opened twice by
        # two concurrent callers (this path and the market-fill path both
        # read the same 'pending'/'active' signal row before either writes).
        with db_module.db() as conn:
            claimed = conn.execute(
                "UPDATE vantage_signals SET status='activating' "
                "WHERE signal_id=? AND status IN ('pending','active')",
                (vantage_sig_id,),
            ).rowcount
        if not claimed:
            return True

        trade_id = str(uuid.uuid4())[:16]
        try:
            # 60min, not the old 240 -- this resting order has no fill-time
            # re-check at all (MT5 fills it directly, no round-trip back to
            # Python), so it shouldn't get the same TTL as a signal that DOES
            # get re-validated at fire time. See core_limit_order_signal.py's
            # _DEFAULT_EXPIRE_MINUTES for the full reasoning (matched here).
            ack = await _ea.place_pending_order(
                trade_id, direction, price, lot_size, stop_loss, tps, pcts, be_at_pos, strategy,
                expire_minutes=60.0, close_full_on_last=True, trail_mode=trail_mode,
            )
        except Exception as exc:
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_signals SET status='pending' WHERE signal_id=? AND status='activating'",
                    (vantage_sig_id,),
                )
            _log.warning("[RE-Engine] place_pending_order failed: %s", exc)
            re_db.update_live_exec(sig["id"], status=f"limit_order_error:{exc}")
            return True

        if ack.get("type") != "pending_order_placed":
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_signals SET status='pending' WHERE signal_id=? AND status='activating'",
                    (vantage_sig_id,),
                )
            _err = ack.get("error", "unknown error")
            _log.warning("[RE-Engine] EA rejected pending order: %s", _err)
            re_db.update_live_exec(sig["id"], status=f"limit_order_rejected:{_err}")
            return True

        ticket = ack.get("ticket")
        now = time.time()
        with db_module.db() as conn:
            conn.execute(
                """INSERT INTO vantage_pending_orders
                   (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                    tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,strategy)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trade_id, vantage_sig_id, None, "Reversal Engine", direction, price, stop_loss,
                 json.dumps(tps), json.dumps(pcts), be_at_pos, 0, lot_size, ticket,
                 "working", now, strategy),
            )
        re_db.update_live_exec(sig["id"], vantage_sig_id=vantage_sig_id, status=f"limit_order_placed:{ticket}")
        _log.info("[RE-Engine] pending %s ticket=%s @ %.2f strategy=%s", direction, ticket, price, strategy)
        return True
