"""
Walk-forward backtest harness for the breakout engine.

Added 2026-07-16 alongside the entry-logic rebuild so future recalibrations
are validated against real candle history BEFORE they touch live trading —
the previous tuning loop (nightly AI batch analysis adjusting params from
small recent samples) ratcheted the engine into a losing configuration with
no counterfactual check.

Usage (from a script or REPL):

    from backend.src.services.breakout_signal.backtest import run_walkforward, report

    candles = {...}   # {"M5": [...], "M1": [...], "H1": [...], "H4": [...]}
                      # each candle: {"time": unix_utc, "open","high","low","close"}
    trades = run_walkforward(candles)                 # current live params
    trades = run_walkforward(candles, overrides={"compression_max_range_atr": 3.0})
    print(report(trades))

Candle data comes from an MT5 history dump (see scratchpad vps_dump_candles.py
pattern: copy_rates_range with the broker UTC+3 offset corrected to true UTC).

Simulator semantics (matches engine._check_outcomes):
  - thirds scale-out at TP1/TP2/TP3 (tp1_mult/tp2_mult/tp3_mult × SL distance)
  - SL → BE+cost after TP1, SL → TP1 after TP2
  - time stop after time_stop_mins if unrealized < -0.2 × SL distance
  - conservative intrabar tie-break: SL fills before TP within the same M1 bar
Not modelled: news windows, spread gate, Claude review, ML gate — all of which
only REMOVE trades, so live selectivity is >= backtest selectivity.
"""
from __future__ import annotations

import bisect
from datetime import datetime, timezone
from typing import Optional

from backend.src.services.breakout_signal import adaptive_params as ap
from backend.src.services.breakout_signal.signal_generator import (
    check_breakout_go, check_breakout_retest, calculate_breakout_risk_levels,
)
from backend.src.services.test_signal.signal_generator import (
    compute_htf_bias, compute_h4_bias, compute_adx, compute_macd_hist,
    identify_key_levels, compute_atr,
)

COST_PTS = 0.35          # spread + fees per round trip, points
LOT_DOLLARS_PER_PT = 10.0  # 0.1 lot on XAUUSD

_BLOCKED_HOURS_UTC = frozenset({12, 13, 14})
_BLOCKED_LEVEL_TYPES = frozenset({"fvg_bull", "round"})
_LEVEL_COOLDOWN_S = 35 * 60


def _session_at(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    dow, h = dt.weekday(), dt.hour
    if dow == 5 or (dow == 6 and h < 22):
        return "closed"
    if 23 <= h or h < 8:
        return "asian"
    if 8 <= h < 12:
        return "london"
    if 12 <= h < 17:
        return "overlap"
    if 17 <= h < 21:
        return "ny"
    return "off"


def _simulate(direction: str, entry: float, sl_dist: float, t0: float,
              m1: list[dict], m1_times: list[float],
              get=None) -> tuple[float, float]:
    """Replay one trade on M1 candles. Returns (net_points, exit_ts)."""
    get = get or ap.get
    sgn = 1 if direction == "BUY" else -1
    tps = [entry + sgn * sl_dist * get(k) for k in ("tp1_mult", "tp2_mult", "tp3_mult")]
    fracs = [0.33, 0.33, 0.34]
    ts_mins = get("time_stop_mins")
    cur_sl = entry - sgn * sl_dist
    banked, remaining, tp_hit = 0.0, 1.0, 0

    i = bisect.bisect_left(m1_times, t0)
    for c in m1[i:]:
        ts = c["time"]
        hi, lo = c["high"], c["low"]
        if remaining >= 0.999 and ts - t0 > ts_mins * 60:
            unreal = sgn * (c["close"] - entry)
            if unreal < -0.2 * sl_dist:
                return banked + (unreal - COST_PTS) * remaining, ts
        if (lo <= cur_sl) if direction == "BUY" else (hi >= cur_sl):
            return banked + (sgn * (cur_sl - entry) - COST_PTS) * remaining, ts
        while tp_hit < 3:
            tp = tps[tp_hit]
            if not ((hi >= tp) if direction == "BUY" else (lo <= tp)):
                break
            leg = sgn * (tp - entry)
            if tp_hit == 2:
                return banked + (leg - COST_PTS) * remaining, ts
            banked += (leg - COST_PTS) * fracs[tp_hit]
            remaining -= fracs[tp_hit]
            cur_sl = (entry + sgn * COST_PTS) if tp_hit == 0 else tps[0]
            tp_hit += 1
    last = m1[-1] if m1 else {"close": entry, "time": t0}
    return banked + (sgn * (last["close"] - entry) - COST_PTS) * remaining, last["time"]


def run_walkforward(candles: dict, overrides: Optional[dict] = None) -> list[dict]:
    """Walk M5 history through the CURRENT detection code and simulate each
    signal on M1. `overrides` temporarily overrides adaptive params (defaults +
    DB values otherwise). Returns a list of trade dicts."""
    m5, m1 = candles["M5"], candles["M1"]
    h1, h4 = candles["H1"], candles["H4"]
    m1_times = [c["time"] for c in m1]
    h1_times = [c["time"] for c in h1]
    h4_times = [c["time"] for c in h4]

    base_get = ap.get

    def get(key):
        if overrides and key in overrides:
            return float(overrides[key])
        try:
            return base_get(key)
        except Exception:
            # Running outside the app (no initialized bo_config DB) — use the
            # code defaults, which is what a fresh install would run with.
            return float(ap.PARAMS[key]["default"])

    ap_get_orig, ap.get = ap.get, get

    try:
        trades: list[dict] = []
        open_until = 0.0
        cooldowns: dict[tuple, float] = {}

        for i in range(320, len(m5) - 1):
            ts = m5[i]["time"] + 300
            if ts < open_until:
                continue
            sess = _session_at(ts)
            if sess == "closed":
                continue
            if datetime.fromtimestamp(ts, tz=timezone.utc).hour in _BLOCKED_HOURS_UTC:
                continue

            m5c = m5[max(0, i - 79):i + 1]
            atr = compute_atr(m5c[-20:], period=14)
            if atr <= 0:
                continue
            atr_ref = compute_atr(m5c[-50:-10], period=14) if len(m5c) >= 50 else atr
            if atr_ref > 0 and atr / atr_ref < 0.65:
                continue

            hi1 = bisect.bisect_right(h1_times, ts - 3600)
            h1c = h1[max(0, hi1 - 120):hi1]
            if len(h1c) < 60:
                continue
            hi4 = bisect.bisect_right(h4_times, ts - 14400)
            h4c = h4[max(0, hi4 - 40):hi4]

            htf = compute_htf_bias(h1c)
            h4b = compute_h4_bias(h4c) if h4c else "neutral"
            adx = compute_adx(m5c[-30:])
            _, macd = compute_macd_hist([c["close"] for c in m5c])
            price = m5[i]["close"]

            if adx < min(ap.get("min_adx_go"), ap.get("min_adx_retest")):
                continue
            if htf == "neutral" and adx < 40:
                continue

            key_levels = identify_key_levels(h1c, price, m15_candles=m5c)

            cand = None
            if sess != "asian":
                cand = check_breakout_go(m5c, key_levels, htf, h4b, price, atr, adx, macd)
            if not cand:
                cand = check_breakout_retest(m5c, key_levels, htf, h4b, price, atr, adx, macd)
            if not cand:
                continue
            if cand.get("broken_level_type") in _BLOCKED_LEVEL_TYPES:
                continue

            ck = (cand["direction"], round(cand["broken_level"]))
            if ts - cooldowns.get(ck, 0) < _LEVEL_COOLDOWN_S:
                continue
            cooldowns[ck] = ts

            risk = calculate_breakout_risk_levels(cand, price, atr, adx)
            if not risk:
                continue

            net_pts, exit_ts = _simulate(cand["direction"], risk["entry_mid"],
                                         risk["sl_dist"], ts, m1, m1_times)
            open_until = exit_ts
            trades.append({
                "ts": ts, "direction": cand["direction"],
                "breakout_type": cand["breakout_type"],
                "level_type": cand.get("broken_level_type"),
                "session": sess, "adx": round(adx, 1),
                "sl_dist": round(risk["sl_dist"], 2),
                "net_pts": round(net_pts, 2),
                "usd": round(net_pts * LOT_DOLLARS_PER_PT, 2),
            })
        return trades
    finally:
        ap.get = ap_get_orig


def report(trades: list[dict]) -> str:
    if not trades:
        return "no trades"
    n = len(trades)
    pnl = sum(t["usd"] for t in trades)
    wins = [t for t in trades if t["usd"] > 1.0]
    losses = [t for t in trades if t["usd"] < -1.0]
    aw = sum(t["usd"] for t in wins) / len(wins) if wins else 0.0
    al = sum(t["usd"] for t in losses) / len(losses) if losses else 0.0
    eq = peak = dd = 0.0
    for t in trades:
        eq += t["usd"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return (f"n={n}  pnl=${pnl:+.2f}  wr={100 * len(wins) / n:.1f}%  "
            f"avg_win=${aw:.2f}  avg_loss=${al:.2f}  "
            f"payoff={abs(aw / al) if al else 0:.2f}  maxDD=${dd:.2f}")
