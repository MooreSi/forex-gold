"""
AI quality gate for test signals (Claude or DeepSeek, per Settings > AI).
Reviews a candidate signal and returns approve/reject + rationale.
Optional — if no API key is configured, signals are accepted with a neutral score.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from backend.src.services.ai import provider as ai_provider

log = logging.getLogger("test_signal")

_SYSTEM = (
    "You are a professional XAUUSD (gold) signal analyst reviewing algorithmically generated trade setups. "
    "This system generates REVERSAL / KEY-LEVEL BOUNCE signals — NOT trend-following signals. "
    "The entire edge comes from price reaching a significant level, showing exhaustion, then snapping back.\n\n"

    "APPROVAL FRAMEWORK — what makes a GOOD setup:\n"
    "  + Strong key level: swing pivot or level hit 2+ times before (strength ≥ 2)\n"
    "  + Momentum FADING into the level: RSI divergence, shrinking candle bodies, MACD histogram weakening or reversing\n"
    "  + Clean rejection candle: pin bar / shooting star / engulfing AT the level\n"
    "  + London or NY/overlap session (not Asian — low liquidity, more false moves)\n"
    "  + HTF bias AGAINST the entry direction is acceptable (and often ideal for mean-reversion)\n"
    "  + ADX 20-35 (ranging to mildly trending) — sweet spot for bounces\n\n"

    "REJECTION FRAMEWORK — what makes a BAD setup:\n"
    "  - Weak level: round number only (strength 1), never tested before\n"
    "  - Strong trend WITH momentum: ADX > 45 AND MACD strongly confirming direction = do NOT fade\n"
    "  - No rejection candle: price just drifting sideways through the level\n"
    "  - Asian session with weak level (high risk of fake-out before London opens)\n"
    "  - Entry mid-trend with no divergence — chasing a move already well underway\n\n"

    "CRITICAL INSIGHTS ON MACD (read carefully — this is the most common mistake):\n"
    "  BOUNCE SELL (price rallied to resistance, now fading):\n"
    "    - A POSITIVE MACD at resistance is EXPECTED — price trended up to get there. Not a red flag.\n"
    "    - What matters: is that positive histogram SHRINKING (stalling) or GROWING (breakout)?\n"
    "    - RSI divergence + MACD FADING = ideal. Approve. RSI divergence + MACD GROWING = approve lower score.\n"
    "    - Only reject if: MACD strongly growing AND level weak (strength 1) AND no RSI divergence.\n"
    "  BOUNCE BUY (price sold off to support, now recovering):\n"
    "    - A NEGATIVE MACD at support is EXPECTED — price trended down to get there. Not a red flag.\n"
    "    - What matters: is that negative histogram SHRINKING toward zero (selling fading) or GROWING more negative (breakdown)?\n"
    "    - RSI divergence + MACD FADING (toward zero) = ideal bounce buy. Approve.\n"
    "    - RSI divergence + MACD STILL GROWING negative = higher risk. Strong level (strength ≥ 2) can override. Lower score.\n"
    "    - Only reject if: MACD growing MORE negative AND level weak (strength 1) AND no RSI divergence.\n"
    "  GENERAL: The MACD trend shown as 'FADING' or 'GROWING' is computed from last 6 bars — trust it.\n\n"

    "CALIBRATION NOTE — When uncertain, approve with lower score rather than reject:\n"
    "  This system is in training-data collection mode. False rejections waste signals. "
    "If a setup has a strong level (strength ≥ 2) OR RSI divergence, lean toward approval. "
    "Reserve outright rejection for clearly broken setups: weak level + strong trend + no divergence.\n\n"

    "Respond ONLY with a single minified JSON object matching this exact schema — no other text:\n"
    '{"approved":true,"quality_score":0.0,"confidence":"low|medium|high",'
    '"rationale":"one concise sentence","risk_factors":["string"],'
    '"improvement":"one suggestion or empty string"}'
)


async def review_signal(
    candidate: dict,
    htf_bias: str,
    session: str,
    key_levels: list[dict],
    m15_recent_closes: list[float],
    cfg: dict,
    timeout: int = 20,
    tg_signals: list[dict] | None = None,
    ml_prob: float | None = None,
    trigger_pattern: str = "bounce",
    h4_bias: str = "neutral",
    adx: float = 0.0,
    macd_hist: float = 0.0,
    macd_hist_trend: list[float] | None = None,
    regime: str = "neutral",
) -> dict:
    """
    Ask the configured AI provider to review a candidate signal.
    Returns dict with: approved, quality_score, confidence, rationale.
    Falls back to auto-approve with neutral score if API unavailable.
    """
    # No API key: user opted out of AI review — auto-approve and continue.
    no_key_fallback = {
        "approved": True,
        "quality_score": 0.70,
        "confidence": "low",
        "rationale": "AI review not configured — auto-approved, rules-based filters passed.",
        "risk_factors": [],
        "improvement": "",
        "_is_fallback": True,
        "_fallback_reason": "no_api_key",
    }

    if not ai_provider.is_configured(cfg):
        return no_key_fallback

    direction = candidate["direction"]
    entry_mid = candidate["entry_mid"]
    sl        = candidate["stop_loss"]
    tp1       = candidate.get("tp1")
    tp3       = candidate.get("tp3")
    sl_dist   = candidate.get("sl_dist", 0)
    rr_tp1    = candidate.get("rr_tp1", 0)
    rr_tp3    = candidate.get("rr_tp3", 0)
    level_type = candidate.get("key_level_type", "")
    level_str  = candidate.get("level_strength", 1)

    now_str = datetime.now(timezone.utc).strftime("%A %d %B %Y %H:%M UTC")
    nearby_levels = [f"${lv['price']:.2f} ({lv['type']}, str:{lv.get('strength',1)})" for lv in key_levels[:6]]

    # MACD momentum direction: is it fading (reversing toward zero) or growing?
    _trend = macd_hist_trend or [macd_hist]
    if len(_trend) >= 2:
        _macd_delta = _trend[-1] - _trend[0]
        if macd_hist > 0:
            _macd_dir = "FADING (bullish momentum weakening)" if _macd_delta < -0.05 else "GROWING (bullish momentum building)"
        else:
            _macd_dir = "FADING (bearish momentum weakening)" if _macd_delta > 0.05 else "GROWING (bearish momentum building)"
        _macd_trend_str = f"{' → '.join(f'{v:+.4f}' for v in _trend)}  ← {_macd_dir}"
    else:
        _macd_trend_str = f"{macd_hist:+.4f}"

    # ADX interpretation
    if adx >= 45:
        _adx_note = "STRONG TREND — fading this move is high risk"
    elif adx >= 25:
        _adx_note = "trending — bounces can still work at strong levels"
    else:
        _adx_note = "ranging — ideal for key-level bounces"

    prompt_lines = [
        f"Date/Time: {now_str}",
        f"HTF Bias (H1): {htf_bias}  |  H4 Bias: {h4_bias}",
        f"Session: {session}  |  Market regime: {regime}",
        f"ADX(14): {adx:.1f}  [{_adx_note}]",
        f"MACD histogram trend (6bar→3bar→now): {_macd_trend_str}",
        "",
        f"Candidate Signal: {direction} XAUUSD",
        f"Trigger pattern: {trigger_pattern}",
        f"Entry zone: ${candidate['entry_low']:.2f} – ${candidate['entry_high']:.2f}",
        f"Entry mid: ${entry_mid:.2f}",
        f"Stop Loss: ${sl:.2f}  (distance: {sl_dist:.2f} pts)",
        f"TP1: ${tp1:.2f}  R:R {rr_tp1:.2f}:1",
        f"TP3: ${tp3:.2f}  R:R {rr_tp3:.2f}:1",
        f"Key level: {level_type}  (strength: {level_str} — higher = more reliable)",
        "",
        f"Nearby key levels: {', '.join(nearby_levels) if nearby_levels else 'none'}",
        f"Recent M15 closes (last 8, oldest→newest): {', '.join(f'{v:.2f}' for v in m15_recent_closes[-8:])}",
        f"ML model win-probability: {ml_prob:.1%}" if ml_prob is not None else "ML model: not yet trained",
    ]

    if tg_signals:
        prompt_lines += ["", "Provider signals received in the last 2 hours (for context only):"]
        for s in tg_signals[:3]:
            tps = [f"${s[k]:.2f}" for k in ("tp1", "tp2", "tp3") if s.get(k)]
            line = (
                f"  [{s.get('source_name', 'provider')}] {s.get('direction', '?')} "
                f"entry ${s.get('entry_low', 0):.2f}-${s.get('entry_high', 0):.2f} "
                f"SL ${s.get('stop_loss', 0):.2f} "
                f"TPs {', '.join(tps) if tps else 'n/a'}"
            )
            prompt_lines.append(line)
        prompt_lines.append(
            "Use these as market context to validate direction alignment — "
            "do NOT blindly follow them."
        )

    prompt = "\n".join(prompt_lines)

    try:
        raw = await ai_provider.complete(cfg, _SYSTEM, prompt, max_tokens=500, timeout=timeout)
        # Strip markdown code fences that the model sometimes adds despite instructions
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:])
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        data = json.loads(raw)
        return data
    except asyncio.TimeoutError:
        log.warning("[TestSignal] AI review timeout — signal skipped")
        return {
            "approved": False, "quality_score": 0.0, "confidence": "low",
            "rationale": "AI review timed out — signal skipped.",
            "risk_factors": ["review_timeout"], "improvement": "",
            "_is_fallback": True, "_fallback_reason": "timeout",
        }
    except Exception as e:
        log.warning("[TestSignal] AI review error: %s — signal skipped", e)
        return {
            "approved": False, "quality_score": 0.0, "confidence": "low",
            "rationale": "AI review error — signal skipped.",
            "risk_factors": ["review_error"], "improvement": "",
            "_is_fallback": True, "_fallback_reason": "error",
        }
