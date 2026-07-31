"""
AI-based quality gate for BREAKOUT signals (Claude or DeepSeek, per Settings > AI).

Uses different criteria from the bounce reviewer:
  - MACD growing in direction of trade is GOOD (not a rejection reason)
  - ADX ≥ threshold is REQUIRED (not a rejection reason)
  - Both H1 + H4 bias must align (stricter than bounce)
  - Nearby opposing S/R within TP1 range IS a rejection reason

Completely separate system prompt and evaluation criteria from bounce engine.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from backend.src.services.ai import provider as ai_provider

_log = logging.getLogger("breakout_signal")

_SYSTEM = """You are a professional XAUUSD (gold) breakout trade reviewer.
Your job is to evaluate whether a proposed BREAK-AND-GO or BREAK-AND-RETEST signal
should be taken based on technical quality.

BREAKOUT LOGIC (opposite to bounce/reversal):
- HIGH ADX (≥35) is GOOD — confirms the market is trending
- MACD histogram GROWING in direction of trade is GOOD — momentum confirmation
- H1+H4 bias aligned with trade direction is REQUIRED
- Price closing THROUGH a level with a full body is GOOD
- Nearby opposing S/R within TP1 distance is a PROBLEM
- A very shallow break (< 3pts through the level) is RISKY

ENTRY TYPES:
- break-and-go: entering RIGHT on the break candle — higher urgency, wider spread risk
- break-and-retest: entering at the broken level after pullback — better entry, may miss if move is fast

SCORING:
Score from 0.00 to 1.00. Output exactly:
SCORE: X.XX
VERDICT: APPROVE or REJECT
REASON: one sentence

Approve if score ≥ 0.45. Be concise. Focus on the specific setup quality."""


def _build_prompt(candidate: dict, risk: dict, context: dict) -> str:
    direction  = candidate["direction"]
    bo_type    = candidate["breakout_type"]
    level      = candidate["broken_level"]
    ltype      = candidate.get("broken_level_type", "level")
    strength   = candidate.get("level_strength", 1)
    adx        = context.get("adx", 0)
    macd_hist  = context.get("macd_hist", 0)
    htf_bias   = context.get("htf_bias", "neutral")
    h4_bias    = context.get("h4_bias",  "neutral")
    session    = context.get("session",  "unknown")
    price      = context.get("price",    0)
    atr        = context.get("atr_m15",  0)
    pts_beyond = candidate.get("pts_beyond", 0)
    prior_breaks = candidate.get("prior_break_candles", 0)

    entry = risk["entry_mid"]
    sl    = risk["stop_loss"]
    tp1   = risk["tp1"]
    tp2   = risk["tp2"]
    tp3   = risk["tp3"]
    rr    = risk["rr_tp1"]
    sl_d  = risk["sl_dist"]

    lines = [
        f"BREAKOUT SIGNAL — XAUUSD {direction} ({bo_type.upper()})",
        "",
        f"Session: {session}  |  HTF H1: {htf_bias}  |  H4: {h4_bias}",
        f"ADX: {adx:.1f}  |  MACD hist: {macd_hist:+.4f}  |  ATR(M15): {atr:.2f}",
        f"Current price: ${price:.2f}",
        "",
        f"Broken level: ${level:.2f} ({ltype}, strength {strength})",
    ]
    trigger = context.get("trigger", "M5_candle")
    if bo_type == "go":
        trigger_label = "velocity (live level cross)" if trigger == "velocity" else "M5 candle body close"
        lines.append(f"Break margin: {pts_beyond:.1f}pts beyond level via {trigger_label}")
    else:
        lines.append(f"Prior break confirmed by {prior_breaks} candle(s); price now retesting level")

    lines += [
        "",
        "PROPOSED TRADE:",
        f"  Entry:  ${entry:.2f}",
        f"  SL:     ${sl:.2f}  ({sl_d:.1f}pts)",
        f"  TP1:    ${tp1:.2f}  (R:R {rr:.1f}:1)",
        f"  TP2:    ${tp2:.2f}",
        f"  TP3:    ${tp3:.2f}",
        "",
        "Is this a quality breakout entry? Score 0.00-1.00, APPROVE/REJECT, one-sentence reason.",
    ]
    return "\n".join(lines)


async def review_signal(
    candidate: dict,
    risk: dict,
    context: dict,
) -> dict:
    """
    Review a breakout signal candidate with the configured AI provider.
    Returns {"approved": bool, "score": float, "rationale": str, "fallback": bool}
    """
    try:
        import backend.src.config as _cfg_mod
        cfg = {
            "ai_provider":       _cfg_mod.get("ai_provider", "claude"),
            "anthropic_api_key": _cfg_mod.get("anthropic_api_key", ""),
            "claude_model":      _cfg_mod.get("claude_model", ""),
            "deepseek_api_key":  _cfg_mod.get("deepseek_api_key", ""),
            "deepseek_model":    _cfg_mod.get("deepseek_model", ""),
        }
        prompt = _build_prompt(candidate, risk, context)

        text = await ai_provider.complete(cfg, _SYSTEM, prompt, max_tokens=120, timeout=20)
        text = text.strip()
        _log.debug("[BO-AI] response: %s", text)

        score    = 0.0
        approved = False
        rationale = ""

        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("SCORE:"):
                try:
                    score = float(line.split(":", 1)[1].strip())
                except (ValueError, IndexError):
                    pass
            elif line.upper().startswith("VERDICT:"):
                approved = "APPROVE" in line.upper()
            elif line.upper().startswith("REASON:"):
                rationale = line.split(":", 1)[1].strip()

        min_score = 0.45
        try:
            from forex_trader.breakout_signal import adaptive_params as _ap
            min_score = _ap.get("min_quality_score")
        except Exception:
            pass

        if score < min_score:
            approved = False

        return {
            "approved":  approved,
            "score":     round(score, 2),
            "rationale": rationale,
            "fallback":  False,
        }

    except asyncio.TimeoutError:
        _log.warning("[BO-AI] Timeout — auto-rejecting")
        return {"approved": False, "score": 0.0, "rationale": "AI timeout — signal skipped.", "fallback": True}
    except Exception as e:
        _log.error("[BO-AI] Error: %s", e)
        return {"approved": False, "score": 0.0, "rationale": "AI error — signal skipped.", "fallback": True}
