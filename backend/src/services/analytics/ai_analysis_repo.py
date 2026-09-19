"""AI Trade Analysis page's data-gathering reads -- moved verbatim from
frontend/pages/ai_trade_analysis.py (M3 page drain). Each takes an explicit
db_path (the page analyses a chosen environment's DB by path -- one of the
plan's named cross-DB reads) and returns plain dicts with the same derived
metrics the page always computed.
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

# Left behind in frontend/pages/ai_trade_analysis.py when the functions below
# were moved here: _session_from_ts needs datetime/timezone, and
# _gather_channel_data scans reply chains with these two patterns. Copied
# verbatim from that file rather than rewritten -- the page still has its own
# copies, and the two must agree about what counts as a claimed TP or SL hit.
_TP_HIT_RE = re.compile(
    r'TP\s*(\d+)\s*(hit|hitt|done|reached|closed|filled|✅|🎯|🤑)',
    re.IGNORECASE,
)
_SL_HIT_RE = re.compile(
    r'(stop\s*loss|sl|stopped)\s*(hit|hitt|done|reached|triggered)',
    re.IGNORECASE,
)


def _session_from_ts(ts: Optional[float]) -> str:
    if not ts:
        return "Unknown"
    hour = datetime.fromtimestamp(float(ts), tz=timezone.utc).hour
    if   0  <= hour < 7:  return "Asian"
    elif 7  <= hour < 12: return "London"
    elif 12 <= hour < 16: return "London/NY"
    elif 16 <= hour < 21: return "NY"
    else:                  return "Off-hours"


def _gather_channel_data(db_path: str, days: int) -> list[dict]:
    """
    Reads all parsed TG signals in the window, joins to trades and partial closes,
    reconstructs the reply chain, and computes derived metrics.
    Returns one dict per channel.
    """
    since_ts = time.time() - days * 86400

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        # Distinct channels that had signals in the window
        channels = conn.execute("""
            SELECT DISTINCT group_id, group_name
            FROM vantage_tg_signals
            WHERE parsed_at >= ? AND direction IS NOT NULL AND group_id IS NOT NULL
            ORDER BY group_name
        """, (since_ts,)).fetchall()

        result: list[dict] = []

        for ch in channels:
            group_id   = ch["group_id"]
            group_name = ch["group_name"] or group_id

            # All parsed signals for this channel in the window, joined to trades
            rows = conn.execute("""
                SELECT ts.*,
                       st.trade_id,
                       st.entry_price,
                       st.close_price,
                       st.close_time,
                       st.exit_reason,
                       st.net_pnl,
                       st.lot_size,
                       st.sl_moved_to_be,
                       st.open_time,
                       st.status  AS trade_status
                FROM vantage_tg_signals ts
                LEFT JOIN vantage_simulated_trades st ON ts.signal_id = st.signal_id
                WHERE ts.group_id = ? AND ts.parsed_at >= ?
                  AND ts.direction IS NOT NULL
                ORDER BY ts.parsed_at DESC
            """, (group_id, since_ts)).fetchall()

            signals_data: list[dict] = []

            for r in rows:
                s = dict(r)

                # ── Reply chain ───────────────────────────────────────────
                replies = conn.execute("""
                    SELECT telegram_message_id, text, timestamp
                    FROM telegram_messages
                    WHERE reply_to_message_id = ? AND group_id = ?
                    ORDER BY telegram_message_id ASC
                """, (s["tg_message_id"], group_id)).fetchall()

                reply_chain = [
                    {"id": rr["telegram_message_id"],
                     "text": (rr["text"] or "").strip(),
                     "ts": rr["timestamp"]}
                    for rr in replies
                ]

                # ── Claimed TP hits + SL acknowledgement ──────────────────
                claimed_tps: list[int] = []
                claimed_sl = False
                for rr in reply_chain:
                    txt = rr["text"]
                    for m in _TP_HIT_RE.finditer(txt):
                        n = int(m.group(1))
                        if n not in claimed_tps:
                            claimed_tps.append(n)
                    if (_SL_HIT_RE.search(txt) or
                            "SL HIT" in txt.upper() or
                            "STOP HIT" in txt.upper()):
                        claimed_sl = True

                # ── Phantom TP detection ──────────────────────────────────
                exit_reason = (s.get("exit_reason") or "").upper()
                net_pnl = float(s.get("net_pnl") or 0)
                is_phantom = bool(
                    claimed_tps and
                    s.get("trade_id") and         # must have a real trade
                    (exit_reason == "SL" or net_pnl < 0)
                )

                # ── R:R ───────────────────────────────────────────────────
                tps = [s.get(f"tp{i}") for i in range(1, 9) if s.get(f"tp{i}")]
                entry_mid = None
                if s.get("entry_low") and s.get("entry_high"):
                    entry_mid = (float(s["entry_low"]) + float(s["entry_high"])) / 2
                sl_val = float(s["stop_loss"]) if s.get("stop_loss") else None
                sl_distance = abs(entry_mid - sl_val) if (entry_mid and sl_val) else None
                rr_tp1 = rr_last = None
                if sl_distance and sl_distance > 0 and tps:
                    sign = 1 if s["direction"] == "BUY" else -1
                    rr_tp1 = round(sign * (tps[0]  - entry_mid) / sl_distance, 2)
                    rr_last = round(sign * (tps[-1] - entry_mid) / sl_distance, 2)

                # ── Entry drift ───────────────────────────────────────────
                entry_drift_pips = None
                if entry_mid and s.get("entry_price"):
                    entry_drift_pips = round(
                        abs(float(s["entry_price"]) - entry_mid) * 10, 1
                    )

                # ── Session ───────────────────────────────────────────────
                session = _session_from_ts(s.get("parsed_at"))

                # ── Partial closes ────────────────────────────────────────
                partial_closes: list[dict] = []
                if s.get("trade_id"):
                    pcs = conn.execute("""
                        SELECT ts, lots_closed, close_price, pnl, reason
                        FROM vantage_partial_closes
                        WHERE trade_id = ?
                        ORDER BY ts ASC
                    """, (s["trade_id"],)).fetchall()
                    partial_closes = [dict(p) for p in pcs]

                # ── Simulated 50%-at-TP1 P&L ─────────────────────────────
                simulated_pnl = None
                if (s.get("entry_price") and tps and s.get("lot_size") and
                        s.get("close_price") and s.get("direction")):
                    ep    = float(s["entry_price"])
                    tp1   = float(tps[0])
                    cp    = float(s["close_price"])
                    lots  = float(s["lot_size"])
                    sign  = 1 if s["direction"] == "BUY" else -1
                    half  = lots * 0.5
                    simulated_pnl = round(
                        sign * (tp1 - ep) * half * 100 +
                        sign * (cp  - ep) * half * 100,
                        2,
                    )

                signals_data.append({
                    "tg_message_id":   s["tg_message_id"],
                    "signal_id":       s.get("signal_id"),
                    "direction":       s.get("direction"),
                    "entry_low":       s.get("entry_low"),
                    "entry_high":      s.get("entry_high"),
                    "stop_loss":       s.get("stop_loss"),
                    "tps":             tps,
                    "parsed_at":       s.get("parsed_at"),
                    "session":         session,
                    "rr_tp1":          rr_tp1,
                    "rr_last":         rr_last,
                    "reply_chain":     reply_chain,
                    "claimed_tps":     sorted(claimed_tps),
                    "claimed_sl":      claimed_sl,
                    "is_phantom":      is_phantom,
                    "entry_drift_pips": entry_drift_pips,
                    "trade": {
                        "trade_id":      s.get("trade_id"),
                        "entry_price":   s.get("entry_price"),
                        "close_price":   s.get("close_price"),
                        "exit_reason":   s.get("exit_reason"),
                        "net_pnl":       s.get("net_pnl"),
                        "sl_moved_to_be": bool(s.get("sl_moved_to_be")),
                        "open_time":     s.get("open_time"),
                        "close_time":    s.get("close_time"),
                        "lot_size":      s.get("lot_size"),
                        "partial_closes": partial_closes,
                        "status":        s.get("trade_status"),
                    } if s.get("trade_id") else None,
                    "simulated_50pct_tp1_pnl": simulated_pnl,
                })

            # ── Channel-level stats ───────────────────────────────────────
            with_trades = [s for s in signals_data if s["trade"]]
            closed      = [s for s in with_trades if (s["trade"] or {}).get("status") == "closed"]
            wins        = [s for s in closed if float((s["trade"] or {}).get("net_pnl") or 0) > 0]
            sl_hits     = [s for s in closed if (s["trade"] or {}).get("exit_reason") == "SL"]
            phantoms    = [s for s in signals_data if s["is_phantom"]]

            total_pnl = sum(float(s["trade"]["net_pnl"] or 0) for s in closed)
            win_rate  = len(wins) / len(closed) * 100 if closed else 0.0

            # Session P&L breakdown
            session_pnl: dict[str, float] = {}
            for s in closed:
                sess = s["session"]
                session_pnl[sess] = round(
                    session_pnl.get(sess, 0) + float(s["trade"]["net_pnl"] or 0), 2
                )

            # Consecutive loss streaks
            pnl_seq = [float(s["trade"]["net_pnl"] or 0) for s in closed]
            max_consec, curr_consec = 0, 0
            for p in pnl_seq:
                if p < 0:
                    curr_consec += 1
                    max_consec = max(max_consec, curr_consec)
                else:
                    curr_consec = 0

            # BE instruction in reply chains
            be_in_channel = any(
                any(
                    kw in rr["text"].lower()
                    for kw in ("stop loss to entry", "breakeven", "move sl", "move stop",
                                "risk free", "bring sl", "bring stop")
                )
                for s in signals_data for rr in s["reply_chain"]
            )

            # Simulated P&L totals for partial-close model
            actual_pnl_sum    = round(total_pnl, 2)
            simulated_pnl_sum = round(sum(
                s["simulated_50pct_tp1_pnl"] or 0
                for s in closed if s["simulated_50pct_tp1_pnl"] is not None
            ), 2)

            result.append({
                "channel_name": group_name,
                "group_id":     group_id,
                "signals":      signals_data,
                "stats": {
                    "total_signals":           len(signals_data),
                    "signals_with_trades":     len(with_trades),
                    "closed_trades":           len(closed),
                    "wins":                    len(wins),
                    "sl_hits":                 len(sl_hits),
                    "phantom_tp_count":        len(phantoms),
                    "total_pnl":               actual_pnl_sum,
                    "win_rate_pct":            round(win_rate, 1),
                    "be_instructions_in_channel": be_in_channel,
                    "session_pnl":             session_pnl,
                    "max_consecutive_losses":  max_consec,
                    "actual_pnl_sum":          actual_pnl_sum,
                    "simulated_50pct_pnl_sum": simulated_pnl_sum,
                },
            })

        return result

    finally:
        conn.close()



def _gather_strategy_dpm_data(db_path: str, days: int) -> dict:
    """
    Compare fixed-strategy performance against DPM-managed trades.

    DPM trades are identified by presence in dpm_trade_performance.
    Fixed-strategy stats use only trades NOT in that table, so the two
    groups are mutually exclusive.
    """
    since_ts = time.time() - days * 86400

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        # All closed trades in window
        closed_rows = conn.execute("""
            SELECT trade_id, strategy, net_pnl, open_time, close_time, exit_reason
            FROM vantage_simulated_trades
            WHERE status = 'closed' AND close_time >= ?
        """, (since_ts,)).fetchall()
        all_closed = [dict(r) for r in closed_rows]

        # DPM records in window
        dpm_rows = conn.execute("""
            SELECT *
            FROM dpm_trade_performance
            WHERE closed_at IS NOT NULL AND closed_at >= ?
        """, (since_ts,)).fetchall()
        dpm_perf = [dict(r) for r in dpm_rows]
        dpm_ids  = {t["trade_id"] for t in dpm_perf}

        # Split
        dpm_closed   = [t for t in all_closed if t["trade_id"] in dpm_ids]
        fixed_closed = [t for t in all_closed if t["trade_id"] not in dpm_ids]

        def _basic_stats(trades: list[dict]) -> dict:
            if not trades:
                return {"count": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                        "total_pnl": 0.0, "avg_pnl": 0.0, "profit_factor": 0.0,
                        "avg_hold_min": 0, "sl_exits": 0, "be_exits": 0}
            wins   = [t for t in trades if float(t["net_pnl"] or 0) > 0]
            losses = [t for t in trades if float(t["net_pnl"] or 0) < 0]
            total  = sum(float(t["net_pnl"] or 0) for t in trades)
            win_p  = sum(float(t["net_pnl"] or 0) for t in wins)
            loss_p = abs(sum(float(t["net_pnl"] or 0) for t in losses))
            hold_mins = []
            for t in trades:
                if t.get("open_time") and t.get("close_time"):
                    hold_mins.append(
                        (float(t["close_time"]) - float(t["open_time"])) / 60.0
                    )
            return {
                "count":         len(trades),
                "wins":          len(wins),
                "losses":        len(losses),
                "win_rate":      round(len(wins) / len(trades) * 100, 1),
                "total_pnl":     round(total, 2),
                "avg_pnl":       round(total / len(trades), 2),
                "profit_factor": round(win_p / max(loss_p, 0.01), 2),
                "avg_hold_min":  round(sum(hold_mins) / len(hold_mins), 0) if hold_mins else 0,
                "sl_exits":      sum(1 for t in trades if t.get("exit_reason") == "SL"),
                "be_exits":      sum(1 for t in trades if t.get("exit_reason") == "BE"),
            }

        # Per fixed-strategy breakdown
        by_strategy: dict[str, list[dict]] = {}
        for t in fixed_closed:
            s = t["strategy"] or "scale_out"
            by_strategy.setdefault(s, []).append(t)

        strategy_breakdown = [
            {"strategy": s, **_basic_stats(ts)}
            for s, ts in sorted(by_strategy.items())
        ]

        # DPM-specific metrics
        r_multiples = [float(t["r_multiple"] or 0) for t in dpm_perf]
        avg_r = round(sum(r_multiples) / len(r_multiples), 3) if r_multiples else 0.0

        trail_captures = []
        for t in dpm_perf:
            if (t.get("exit_type") == "trail" and
                    float(t.get("peak_pnl") or 0) > 0 and
                    float(t.get("final_pnl") or 0) > 0):
                trail_captures.append(
                    float(t["final_pnl"]) / float(t["peak_pnl"])
                )

        def _breakdown(key: str) -> dict[str, dict]:
            out: dict[str, dict] = {}
            for t in dpm_perf:
                label = t.get(key) or "unknown"
                out.setdefault(label, {"count": 0, "pnl": 0.0, "wins": 0})
                pnl = float(t.get("final_pnl") or 0)
                out[label]["count"] += 1
                out[label]["pnl"]    = round(out[label]["pnl"] + pnl, 2)
                if pnl > 0:
                    out[label]["wins"] += 1
            return out

        cal_count = sum(1 for t in dpm_perf if t.get("used_calibrated"))

        return {
            "days":               days,
            "total_closed":       len(all_closed),
            "fixed_stats":        _basic_stats(fixed_closed),
            "dpm_stats":          _basic_stats(dpm_closed),
            "strategy_breakdown": strategy_breakdown,
            "dpm_detail": {
                "count":               len(dpm_perf),
                "avg_r_multiple":      avg_r,
                "avg_trail_capture":   round(sum(trail_captures) / len(trail_captures), 2)
                                       if trail_captures else None,
                "exit_breakdown":      _breakdown("exit_type"),
                "regime_breakdown":    _breakdown("regime_at_entry"),
                "session_breakdown":   _breakdown("session_at_entry"),
                "calibrated_trades":   cal_count,
                "uncalibrated_trades": len(dpm_perf) - cal_count,
            },
        }
    finally:
        conn.close()


# ── Claude prompt builder ──────────────────────────────────────────────────────


def _gather_signal_generator_data(db_path: str, days: int) -> dict:
    """
    Gather per-strategy (internal signal generator) performance data.
    Splits the window into first-half vs second-half to detect improvement trends.
    Returns a dict ready for Claude analysis.
    """
    since_ts = time.time() - days * 86400
    mid_ts   = since_ts + (days / 2) * 86400

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        # All closed trades from internal engines (not Telegram channels)
        rows = conn.execute("""
            SELECT strategy, net_pnl, open_time, close_time, exit_reason, lot_size,
                   tg_source
            FROM vantage_simulated_trades
            WHERE status = 'closed'
              AND close_time >= ?
              AND (tg_source IS NULL
                   OR tg_source NOT IN (
                       SELECT DISTINCT group_id FROM vantage_tg_signals
                       WHERE group_id IS NOT NULL
                   ))
            ORDER BY open_time ASC
        """, (since_ts,)).fetchall()
        all_trades = [dict(r) for r in rows]

        # Strategy labels for display
        _LABELS = {
            "bounce":           "Bounce Engine",
            "breakout":         "Breakout Engine",
            "gold_diggers_copy": "Reversal Engine",
        }

        def _half_stats(trades: list[dict]) -> dict:
            if not trades:
                return {"count": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                        "sl_exits": 0, "be_exits": 0}
            wins = [t for t in trades if float(t["net_pnl"] or 0) > 0]
            total = sum(float(t["net_pnl"] or 0) for t in trades)
            return {
                "count":    len(trades),
                "win_rate": round(len(wins) / len(trades) * 100, 1),
                "total_pnl": round(total, 2),
                "avg_pnl":  round(total / len(trades), 2),
                "sl_exits": sum(1 for t in trades if t.get("exit_reason") == "SL"),
                "be_exits": sum(1 for t in trades if t.get("exit_reason") == "BE"),
            }

        # Group by strategy
        by_strategy: dict[str, list[dict]] = {}
        for t in all_trades:
            s = t["strategy"] or "unknown"
            by_strategy.setdefault(s, []).append(t)

        engines: list[dict] = []
        for strat, trades in sorted(by_strategy.items()):
            label = _LABELS.get(strat, strat)
            early = [t for t in trades if float(t.get("open_time") or 0) < mid_ts]
            late  = [t for t in trades if float(t.get("open_time") or 0) >= mid_ts]
            engines.append({
                "strategy":   strat,
                "label":      label,
                "all":        _half_stats(trades),
                "early_half": _half_stats(early),
                "late_half":  _half_stats(late),
            })

        return {
            "days":         days,
            "total_trades": len(all_trades),
            "engines":      engines,
        }
    finally:
        conn.close()


_SIGNAL_GEN_SCHEMA = (
    '{"overall_assessment":"string",'
    '"engines":[{'
    '"name":"string","verdict":"string",'
    '"trend":"improving|stable|declining|insufficient_data",'
    '"ml_contribution":"string","self_learning_progress":"string",'
    '"acting_like_pro_trader":false,'
    '"key_strength":"string","key_weakness":"string",'
    '"recommendation":"string"}],'
    '"collective_verdict":"string",'
    '"what_would_make_them_professional":"string"}'
)

_SIGNAL_GEN_SYSTEM = (
    "You are an expert in automated trading systems and machine learning applied to XAUUSD (Gold). "
    "You are given performance data for internal signal generator engines built in Python. "
    "Each engine uses a combination of rule-based logic and machine learning. "
    "Some also use an AI model (Claude or DeepSeek, depending on configuration) for decision support.\n\n"
    "CONTEXT ABOUT EACH ENGINE:\n"
    # The Bounce Engine was described here until 2026-09-19. Its code was
    # deleted on 2026-09-14, so every billable analysis was asking a paid model
    # to judge an engine that does not exist -- and a model asked to assess
    # something absent will say something about it, because that is what it was
    # asked for. Pinned by tests/api/test_ai_prompt_matches_this_build.py.
    "- Breakout Engine: detects breakouts from consolidation ranges. Uses ML to classify "
    "genuine breakouts vs fakeouts, improving with each labelled outcome.\n"
    "- Reversal Engine: reverse-engineers the Gold Diggers VIP telegram signal methodology. "
    "Tracks Asia range, swing levels, and round numbers. Has an ML component that compares "
    "its generated signals against real VIP signals to improve correlation over time.\n\n"
    "Your task is to assess:\n"
    "1. Is each engine improving over time? (compare early vs late window performance)\n"
    "2. Is ML actually helping? What evidence supports or contradicts this?\n"
    "3. Are they beginning to make decisions like a professional XAUUSD trader?\n"
    "4. What is the single most important change that would make each engine more professional?\n\n"
    "Be direct and honest. If an engine has too little data, say so and explain what data volume "
    "would be needed to draw conclusions. If there are no meaningful signals of ML learning, "
    "say that too.\n"
    "Respond ONLY with a single minified JSON object matching this exact schema — no other text:\n"
    + _SIGNAL_GEN_SCHEMA
)


# ── The other two subjects' prompts ──────────────────────────────────────────
#
# Carried over from the NiceGUI page on 2026-09-19. Only the signal-generator
# pair survived the port, and `/api/ai/analyse` sent it for ALL THREE subjects
# -- so asking about Telegram channels handed a paid model a pile of channel
# rows, told it they were engines, and asked it to report on engines.
#
# The wording is the owner's, verbatim. Every numbered rule in the channel
# prompt exists because the model got that judgement wrong without it, and
# rephrasing them would be re-running an experiment somebody already paid for.

_ANALYSIS_SCHEMA = (
    '{"reliability_score":0,'
    '"reliability_label":"string",'
    '"executive_summary":"string",'
    '"phantom_tps":[{"date":"string","direction":"string","claimed":"string",'
    '"actual":"string","pnl":0.0}],'
    '"phantom_tp_pattern":"string",'
    '"sl_management":{"channel_instructs_be":false,"current_weakness":"string",'
    '"recommended_rule":"string","implementation_note":"string"},'
    '"partial_close_model":{"actual_period_pnl":0.0,"simulated_50pct_tp1_pnl":0.0,'
    '"improvement":0.0,"verdict":"string"},'
    '"rr_analysis":{"signals_below_1_to_1":0,"comment":"string","flags":["string"]},'
    '"entry_drift":{"avg_pips":0.0,"worst_case_pips":0.0,"comment":"string"},'
    '"session_analysis":"string",'
    '"consecutive_losses":"string",'
    '"lot_sizing":{"actual_win_rate_pct":0.0,"kelly_fraction_pct":0.0,'
    '"recommended_risk_pct":0.0,"reasoning":"string"},'
    '"overall_recommendation":"string"}'
)

_ANALYSIS_SYSTEM = (
    "You are a professional XAUUSD (Gold) trading signal analyst. "
    "You are given real trade data for a Telegram signal channel: what signals were sent, "
    "what the channel claimed in follow-up messages, and what actually happened in execution.\n\n"
    "CRITICAL CONTEXT — read carefully before analysing:\n"
    "1. TP1 = WIN. A signal is considered to have won if it achieves TP1. Whether the trader "
    "takes all profit at TP1, holds for TP2+, or uses partial closes is purely a risk management "
    "decision that has no bearing on signal quality. Evaluate signal quality on whether TP1 was "
    "reachable — not on how much profit was ultimately captured.\n"
    "2. WIDER SL ANALYSIS. When a trade stops out at SL before reaching TP1, explicitly assess "
    "whether a slightly wider SL (relative to recent ATR or nearby structure) would have kept the "
    "trade alive long enough to reach TP1. Flag specific instances where a wider SL appears "
    "structurally justified. This is important because the current fixed SL placement may be "
    "cutting trades short that the signal correctly called.\n"
    "3. ENTRY DRIFT = LATENCY, NOT SIGNAL FAULT. Any difference between the signal's entry zone "
    "midpoint and the actual execution price is caused by infrastructure latency: Telegram → app "
    "→ MT5 bridge (Wine/CrossOver on Mac) → Vantage broker. This is NOT a signal quality issue. "
    "Report entry drift as infrastructure context only — do not penalise the signal for it.\n"
    "4. The trader manages their own SL strategy independently. SL hits reflect risk management "
    "decisions, not solely channel failure.\n"
    "5. Signals provide an entry zone (price range), not a single price. Strong entry-zone "
    "positioning relative to structure is evidence of signal quality.\n"
    "6. A channel with net positive P&L is providing real value even if some trades SL out.\n\n"
    "Analyse across these dimensions:\n"
    "1. Signal quality — entry zone accuracy; R:R at signal; whether TP1 was achievable\n"
    "2. SL placement — were stops too tight? Would wider SLs have captured more TP1s?\n"
    "3. Phantom TPs — channel claims TP hit but trade actually ended at SL or in a loss\n"
    "4. Partial close model — what would 50%-at-TP1 + SL-to-BE have produced vs actual\n"
    "5. Session bias — which time-of-day sessions produce the best and worst results\n"
    "6. Consecutive loss patterns — clusters that suggest adverse regime conditions\n"
    "7. Lot sizing — based on actual (not claimed) win rate, what risk % is appropriate\n"
    "8. Overall profitability assessment — honest verdict on whether this channel adds value\n\n"
    "Be balanced: acknowledge strengths as clearly as you flag weaknesses. "
    "Give concrete rules with numbers where improvements are warranted. "
    "Respond ONLY with a single minified JSON object matching this exact schema — no other text:\n"
    + _ANALYSIS_SCHEMA
)

_STRATEGY_DPM_SCHEMA = (
    '{"overall_verdict":"string",'
    '"best_approach":"dpm|scale_out|be_runner|trail_stop|protected_scale|insufficient_data",'
    '"dpm_assessment":{"verdict":"string","strength":"string","weakness":"string",'
    '"best_regime":"string","best_session":"string",'
    '"recommendation":"keep_dpm|disable_dpm|use_dpm_selectively"},'
    '"strategy_notes":[{"strategy":"string","verdict":"string"}],'
    '"when_to_use_fixed":"string",'
    '"actionable_advice":"string"}'
)

_STRATEGY_DPM_SYSTEM = (
    "You are a professional trading systems analyst evaluating whether an adaptive Dynamic Position "
    "Management (DPM) system outperforms fixed exit strategies on XAUUSD (Gold).\n\n"
    "DPM uses ATR-based trailing stops, adaptive breakeven triggers, and partial close sizing that "
    "self-calibrate over time based on market regime (trending/ranging/spike), session, and momentum.\n\n"
    "Fixed strategies:\n"
    "  scale_out: scale out 20% at each TP, move SL to BE after TP1\n"
    "  be_runner: move SL to BE/TP levels, hold full position to final TP\n"
    "  trail_stop: trailing stop activates after TP1 is hit\n"
    "  protected_scale: hold through TP1+TP2 for BE protection, scale from TP3\n\n"
    "Be direct. Give a clear verdict. If data is thin (< 10 trades), say so and note "
    "what additional data is needed for a firm conclusion.\n"
    "Respond ONLY with a single minified JSON object matching this exact schema — no other text:\n"
    + _STRATEGY_DPM_SCHEMA
)

# Keyed by the subject id `/api/ai/analyse` takes. A KeyError here is the
# second line of defence behind the router's own refusal: a subject with no
# prompt must never fall back to another subject's, because that sends a paid
# model the wrong question with no trace.
SYSTEM_PROMPTS = {
    "channels": _ANALYSIS_SYSTEM,
    "strategies": _STRATEGY_DPM_SYSTEM,
    "generator": _SIGNAL_GEN_SYSTEM,
}
