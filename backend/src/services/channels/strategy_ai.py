"""
Channel-strategy AI evaluator.

Evaluates current market conditions (session, ATR, ADX, RSI, recent price
action) and uses Claude to recommend the optimal strategy per channel.
Results are stored in channel_strategy_rec and consumed by
open_trade_from_signal when a channel is set to Auto mode.

Two entry points:
  evaluate_channels()        — 30-min background cycle (Sonnet)
  evaluate_signal_strategy() — per-signal quick call on signal arrival (Haiku)

Regime logic (research-backed, XAUUSD-specific):
  no_sl_scale    — London/NY overlap (12-16 UTC), ADX > 25, H1 ATR 15-35 pts
  protected_scale — London pre-overlap (07-12 UTC), ADX 20-25, trend forming
  conservative   — Asian session, ADX < 20, ATR > 35 pts, or near news event
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Regime thresholds (XAUUSD H1, pts) ─────────────────────────────────────
_ADX_TREND_MIN    = 25.0
_ADX_WEAK_MAX     = 20.0
_ATR_LOW          = 15.0
_ATR_HIGH         = 35.0
_ATR_DANGER       = 45.0

_OVERLAP_START_UTC = 12
_OVERLAP_END_UTC   = 16
_LONDON_START_UTC  = 7
_ASIAN_END_UTC     = 7

# Minimum confidence to fire a Telegram notification on strategy change
_NOTIFY_CONF_MIN = 0.70


def _utc_hour() -> int:
    return datetime.now(timezone.utc).hour


def classify_regime(atr_h1: float | None, adx_h1: float | None) -> str:
    """
    Fast rule-based regime classifier.  Used as fallback when Claude is
    unavailable and as pre-filter context for the Claude prompt.
    """
    hour = _utc_hour()
    atr  = atr_h1 or 20.0
    adx  = adx_h1 or 18.0

    if atr > _ATR_DANGER:
        return "conservative"
    if hour < _ASIAN_END_UTC:
        return "conservative"

    if _OVERLAP_START_UTC <= hour < _OVERLAP_END_UTC:
        if adx >= _ADX_TREND_MIN and _ATR_LOW <= atr <= _ATR_HIGH:
            return "no_sl_scale"
        if adx >= _ADX_WEAK_MAX:
            return "protected_scale"
        return "conservative"

    if _LONDON_START_UTC <= hour < _OVERLAP_START_UTC:
        if adx >= _ADX_TREND_MIN and atr <= _ATR_HIGH:
            return "protected_scale"
        return "conservative"

    return "conservative"


# ── Market context helpers ───────────────────────────────────────────────────

def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder-smoothed RSI. Returns None when there is insufficient data."""
    if len(closes) < period + 2:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _price_narrative(candles: list[dict], n: int = 3) -> str:
    """One-line description of the last n H1 candles for the Claude prompt."""
    if not candles or len(candles) < 2:
        return "insufficient candle data"
    recent = candles[-n:]
    parts: list[str] = []
    for c in recent:
        o  = float(c.get("open",  0) or 0)
        cl = float(c.get("close", 0) or 0)
        size  = round(abs(cl - o), 1)
        arrow = "+" if cl >= o else "-"
        parts.append(f"{arrow}{size}pt")
    highs = [float(c.get("high", 0) or 0) for c in candles[-6:]]
    lows  = [float(c.get("low",  0) or 0) for c in candles[-6:]]
    if len(highs) >= 3:
        if highs[-1] > highs[-2] > highs[-3] and lows[-1] > lows[-2] > lows[-3]:
            structure = "HH/HL — bullish"
        elif highs[-1] < highs[-2] < highs[-3] and lows[-1] < lows[-2] < lows[-3]:
            structure = "LH/LL — bearish"
        else:
            structure = "mixed"
    else:
        structure = "unknown"
    return f"{' '.join(parts)} | structure: {structure}"


# ── 30-minute channel evaluation (Sonnet) ───────────────────────────────────

async def evaluate_channels(engine, cfg: dict) -> dict[str, dict]:
    """
    Fetch live market context and channel performance, call the configured AI
    provider, store per-channel recommendations, and send a Telegram alert
    when the recommended strategy changes for an Auto channel.

    Returns {source: {strategy, reasoning, confidence}}.
    """
    from backend.src.services.ai import provider as ai_provider
    from forex_trader.core import database as _db
    from backend.src.utils.models import STRATEGY_NAMES

    results: dict[str, dict] = {}

    # ── Gather market context ────────────────────────────────────────────────
    atr_h1 = adx_h1 = atr_m15 = rsi_h1 = spread_pts = None
    price_narrative = "unavailable"
    try:
        tick = await engine.get_tick()
        if tick:
            spread_pts = round(tick.spread_points, 2)

        h1_candles = await engine._bridge.get_candles("H1", 30)
        if h1_candles and len(h1_candles) >= 14:
            highs  = [float(c["high"])  for c in h1_candles]
            lows   = [float(c["low"])   for c in h1_candles]
            closes = [float(c["close"]) for c in h1_candles]

            trs = [max(highs[i] - lows[i],
                       abs(highs[i] - closes[i-1]),
                       abs(lows[i]  - closes[i-1]))
                   for i in range(1, min(15, len(h1_candles)))]
            atr_h1 = round(sum(trs) / len(trs), 2)

            if len(h1_candles) >= 28:
                plus_dms  = [max(highs[i]-highs[i-1], 0) for i in range(1, 29)]
                minus_dms = [max(lows[i-1]-lows[i],   0) for i in range(1, 29)]
                trs28     = [max(highs[i]-lows[i],
                                 abs(highs[i]-closes[i-1]),
                                 abs(lows[i]-closes[i-1]))
                             for i in range(1, 29)]
                atr28 = sum(trs28[-14:]) / 14
                if atr28 > 0:
                    pdi = sum(plus_dms[-14:])  / 14 / atr28 * 100
                    mdi = sum(minus_dms[-14:]) / 14 / atr28 * 100
                    dx  = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
                    adx_h1 = round(dx, 1)

            rsi_h1          = _compute_rsi(closes)
            price_narrative = _price_narrative(h1_candles)

        m15_candles = await engine._bridge.get_candles("M15", 20)
        if m15_candles and len(m15_candles) >= 14:
            m_h = [float(c["high"])  for c in m15_candles]
            m_l = [float(c["low"])   for c in m15_candles]
            m_c = [float(c["close"]) for c in m15_candles]
            m_trs = [max(m_h[i] - m_l[i],
                         abs(m_h[i] - m_c[i-1]),
                         abs(m_l[i] - m_c[i-1]))
                     for i in range(1, min(15, len(m15_candles)))]
            atr_m15 = round(sum(m_trs) / len(m_trs), 2)

    except Exception as exc:
        log.debug("channel_strategy_ai: market data fetch failed: %s", exc)

    rule_regime   = classify_regime(atr_h1, adx_h1)
    hour_utc      = _utc_hour()
    session_label = (
        "Asian"               if hour_utc < 7  else
        "London"              if hour_utc < 12 else
        "Overlap (London+NY)" if hour_utc < 16 else
        "NY afternoon"
    )

    # ── Gather channel performance + open trade counts ───────────────────────
    channels_data    = _db.get_all_channel_strategy_settings()
    valid_strategies = list(STRATEGY_NAMES.keys())

    channel_lines: list[str] = []
    for ch in channels_data:
        src     = ch["source"]
        rec     = _db.get_channel_strategy_rec(src)
        ov      = ch.get("strategy_override")
        is_auto = ch.get("auto_strategy", False)
        open_n  = _db.get_open_trade_count_for_channel(src)
        mode_tag = "AUTO" if is_auto else (f"manual:{ov}" if ov else "inherit_global")
        channel_lines.append(
            f'  "{src}": WR={ch["win_rate"]:.1f}% n={ch["sample_n"]} '
            f'PnL=${ch["net_pnl"]:.2f} open_trades={open_n} '
            f'mode={mode_tag} last_rec={rec.get("strategy") or "none"}'
        )

    strat_desc = "\n".join(
        f"  {k}: {v}" for k, v in STRATEGY_NAMES.items()
    )

    rsi_str     = f"{rsi_h1:.1f}"   if rsi_h1   is not None else "unavailable"
    atr_m15_str = f"{atr_m15:.2f} pts" if atr_m15 is not None else "unavailable"

    prompt = f"""You are a forex trading strategy advisor for XAUUSD (gold) signals.

CURRENT MARKET CONDITIONS:
- Session: {session_label} (UTC hour: {hour_utc})
- H1 ATR: {atr_h1 if atr_h1 is not None else "unavailable"} pts  (H1 volatility baseline)
- M15 ATR: {atr_m15_str}  (short-term; if M15 ATR > H1 ATR, volatility is expanding intrabar)
- H1 ADX: {adx_h1 if adx_h1 is not None else "unavailable"}  (>25 trending, <20 ranging)
- H1 RSI: {rsi_str}  (>70 overbought, <30 oversold, 40-60 neutral)
- Current spread: {spread_pts if spread_pts is not None else "unavailable"} pts
- Rule-based regime: {rule_regime}
- Recent H1 price action (last 3 candles): {price_narrative}

AVAILABLE STRATEGIES:
{strat_desc}

CHANNEL PERFORMANCE (last 30 days):
{chr(10).join(channel_lines) if channel_lines else "  No data yet"}

STRATEGY SELECTION RULES:
- Channels with open_trades > 0: keep the SAME strategy as last_rec (no disruption mid-trade)
- signal_climber: use for professional multi-TP signals (GD2, GDV) with 4+ TPs; TP1 R:R < 1.0 is fine
- no_sl_scale: ONLY during Overlap (UTC 12-16) AND ADX > 25 AND H1 ATR 15-35 pts
- protected_scale: London session, ADX 20-25, trend developing
- trail_stop: ADX > 25, M15 ATR expanding, clear directional structure
- conservative: Asian session, ATR > 35 (spike risk), RSI extreme (>75 or <25), or channel WR < 55%
- Channels with WR > 65% and good momentum: can use aggressive strategies

TASK: Recommend the single best strategy per channel for current conditions.

Respond ONLY with a JSON object — no prose before or after:
{{
  "<channel_source>": {{
    "strategy": "<strategy_key>",
    "reasoning": "<one sentence, max 120 chars>",
    "confidence": <0.0-1.0>
  }},
  ...
}}

Use only these strategy keys: {json.dumps(valid_strategies)}
If a channel has no performance data, recommend "conservative" with confidence 0.4."""

    # ── Snapshot current recs before updating (for change detection) ─────────
    prev_recs: dict[str, str] = {
        ch["source"]: _db.get_channel_strategy_rec(ch["source"]).get("strategy", "")
        for ch in channels_data
    }

    # ── Call the configured AI provider ──────────────────────────────────────
    if not ai_provider.is_configured(cfg):
        log.info("channel_strategy_ai: no API key — using rule-based fallback")
        for ch in channels_data:
            src       = ch["source"]
            reasoning = (
                f"Rule: {session_label}, ATR={atr_h1 or '?'}, ADX={adx_h1 or '?'} → {rule_regime}"
            )
            _db.set_channel_strategy_rec(src, rule_regime, reasoning, 0.6)
            results[src] = {"strategy": rule_regime, "reasoning": reasoning, "confidence": 0.6}
        return results

    try:
        raw = await ai_provider.complete(cfg, "", prompt, max_tokens=1024, timeout=30)
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        for src, rec in data.items():
            strategy   = rec.get("strategy", rule_regime)
            reasoning  = (rec.get("reasoning") or "")[:200]
            confidence = float(rec.get("confidence", 0.7))
            if strategy not in valid_strategies:
                strategy = rule_regime
            _db.set_channel_strategy_rec(src, strategy, reasoning, confidence)
            results[src] = {"strategy": strategy, "reasoning": reasoning, "confidence": confidence}
        log.info(
            "channel_strategy_ai: evaluated %d channels (provider=%s model=%s)",
            len(results), cfg.get("ai_provider", "claude"),
            cfg.get("claude_model") if cfg.get("ai_provider", "claude") != "deepseek" else cfg.get("deepseek_model"),
        )

        # ── Telegram notification on strategy change for Auto channels ───────
        _changed: list[str] = []
        for ch in channels_data:
            src = ch["source"]
            if not ch.get("auto_strategy"):
                continue
            if src not in results:
                continue
            new_strat = results[src]["strategy"]
            old_strat = prev_recs.get(src, "")
            conf      = results[src]["confidence"]
            if new_strat != old_strat and old_strat and conf >= _NOTIFY_CONF_MIN:
                from backend.src.utils.models import STRATEGY_NAMES as _SN
                old_label = _SN.get(old_strat, old_strat)
                new_label = _SN.get(new_strat, new_strat)
                _changed.append(
                    f"*{src}*: {old_label} → {new_label} ({conf:.0%})\n"
                    f"_{results[src]['reasoning']}_"
                )
        if _changed:
            import asyncio
            from backend.src.services.telegram import alerts as telegram_alerts
            body = "\n\n".join(_changed)
            asyncio.create_task(telegram_alerts.send_message(
                f"Auto strategy updated ({session_label}):\n\n{body}",
                None, "auto_strategy_change",
            ))

    except Exception as exc:
        log.warning("channel_strategy_ai: Claude call failed: %s — using rule fallback", exc)
        for ch in channels_data:
            src       = ch["source"]
            reasoning = f"Rule fallback ({exc.__class__.__name__}): {rule_regime}"
            _db.set_channel_strategy_rec(src, rule_regime, reasoning, 0.5)
            results[src] = {"strategy": rule_regime, "reasoning": reasoning, "confidence": 0.5}

    return results


# ── Per-signal quick evaluation (Haiku) ─────────────────────────────────────

async def evaluate_signal_strategy(
    engine,
    signal: dict,
    channel: str,
    cfg: dict,
) -> dict:
    """
    Quick per-signal strategy decision using the configured AI provider's
    fast/cheap model.

    Called when a TG signal arrives on an Auto channel before opening a trade.
    Returns {strategy, reasoning, confidence, skip}.
    skip=True means the AI recommends not trading this specific signal.
    """
    from backend.src.services.ai import provider as ai_provider
    from forex_trader.core import database as _db
    from backend.src.utils.models import STRATEGY_NAMES

    valid_strategies = list(STRATEGY_NAMES.keys())
    current_rec      = _db.get_channel_strategy_rec(channel)

    direction  = (signal.get("direction") or "?").upper()
    entry_low  = float(signal.get("entry_low",  0) or 0)
    entry_high = float(signal.get("entry_high", 0) or 0)
    stop_loss  = float(signal.get("stop_loss",  0) or 0)
    entry_mid  = (entry_low + entry_high) / 2.0
    zone_width = round(abs(entry_high - entry_low), 2)
    sl_dist    = round(abs(entry_mid - stop_loss), 2)

    tps: list[float] = []
    for i in range(1, 9):
        v = signal.get(f"tp{i}")
        if v is None:
            break
        tps.append(float(v))

    tp1_rr   = round(abs(tps[0]  - entry_mid) / sl_dist, 2) if (tps and sl_dist > 0) else None
    final_rr = round(abs(tps[-1] - entry_mid) / sl_dist, 2) if (tps and sl_dist > 0) else None

    ch_data = next(
        (c for c in _db.get_all_channel_strategy_settings() if c["source"] == channel), {}
    )

    prompt = f"""You are a forex strategy selector for XAUUSD gold signals. Pick the best strategy for this specific signal.

CHANNEL: {channel}
Performance: WR={ch_data.get('win_rate', 0):.1f}% n={ch_data.get('sample_n', 0)} PnL=${ch_data.get('net_pnl', 0):.2f}
30-min recommendation: {current_rec.get("strategy") or "none"} — {current_rec.get("reasoning") or "no context"}

SIGNAL GEOMETRY:
- Direction: {direction}
- Entry zone: ${entry_low:.2f}–${entry_high:.2f} (width: {zone_width:.1f} pts)
- SL distance from entry mid: {sl_dist:.1f} pts
- Take profits ({len(tps)} levels): {[round(t, 2) for t in tps]}
- TP1 R:R: {tp1_rr if tp1_rr is not None else "N/A"}
- Final TP R:R: {final_rr if final_rr is not None else "N/A"}

STRATEGY SELECTION RULES:
- signal_climber: best when 4+ TPs and professional ladder; TP1 R:R < 1.0 is ACCEPTABLE (TP6 is the real target)
- trail_stop: good for 1-2 TP signals in trending market
- conservative: fewer than 3 TPs, or TP1 R:R < 0.6
- no_sl_scale: only if current 30-min rec already says no_sl_scale
- Set skip=true ONLY if signal geometry is clearly invalid (SL on wrong side, zone inverted)

Available strategies: {json.dumps(valid_strategies)}

Respond ONLY with JSON, no prose:
{{"strategy": "<key>", "reasoning": "<max 100 chars>", "confidence": <0.0-1.0>, "skip": false}}"""

    if not ai_provider.is_configured(cfg):
        strat = current_rec.get("strategy") or "conservative"
        return {"strategy": strat, "reasoning": "No API key — using 30-min rec", "confidence": 0.5, "skip": False}

    try:
        # Previously hardcoded to claude-haiku-4-5-20251001 regardless of the
        # user's configured model — now uses whichever provider/model is
        # selected in Settings > AI, like every other AI call in the app.
        raw = await ai_provider.complete(cfg, "", prompt, max_tokens=256, timeout=20)
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        data       = json.loads(raw)
        strategy   = data.get("strategy") or current_rec.get("strategy") or "conservative"
        reasoning  = (data.get("reasoning") or "")[:200]
        confidence = float(data.get("confidence", 0.6))
        skip       = bool(data.get("skip", False))
        if strategy not in valid_strategies:
            strategy = current_rec.get("strategy") or "conservative"
        return {"strategy": strategy, "reasoning": reasoning, "confidence": confidence, "skip": skip}
    except Exception as exc:
        log.warning("evaluate_signal_strategy failed: %s — using 30-min rec", exc)
        strat = current_rec.get("strategy") or "conservative"
        return {"strategy": strat, "reasoning": f"Fallback ({exc.__class__.__name__})", "confidence": 0.4, "skip": False}
