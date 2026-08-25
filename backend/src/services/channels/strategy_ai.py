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
    from backend.src.db import database as _db
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
    # Regime from the SAME classifier core_auto_template's mapping was
    # backtested against -- dpm_engine.detect_regime over the engine's own
    # M5/30 window. Deliberately not classify_regime() above (H1 ATR/ADX):
    # if the AI reasoned about one regime label while the deterministic
    # baseline and the auto loop used another, an override would look
    # arbitrary and be impossible to audit.
    from backend.src.services.dpm.engine import (
        detect_regime as _detect_regime, compute_atr as _dpm_atr,
    )
    _dpm = list(getattr(engine, "_dpm_candles", None) or [])
    try:
        _auto_regime = (_detect_regime(_dpm, _dpm_atr(_dpm))
                        if len(_dpm) >= 20 else rule_regime)
    except Exception:
        _auto_regime = rule_regime
    hour_utc      = _utc_hour()
    session_label = (
        "Asian"               if hour_utc < 7  else
        "London"              if hour_utc < 12 else
        "Overlap (London+NY)" if hour_utc < 16 else
        "NY afternoon"
    )

    # ── Gather channel performance + open trade counts ───────────────────────
    channels_data    = _db.get_all_channel_strategy_settings()
    # Built-in strategies PLUS the regime-tuned EA templates and the
    # stand-down option (2026-08-14). Previously this was STRATEGY_NAMES
    # alone and the validator below silently rewrote anything else back to
    # the rule-based regime pick -- so a template could never be
    # recommended no matter what the model returned, and Auto mode could
    # only ever select built-in strategies.
    from backend.src.services.positions import core_auto_template as _auto
    # Templates and stand_down ONLY -- deliberately NOT the built-in strategies
    # (2026-08-17). Auto mode is defined by the backtested template map, and
    # every other layer of it already speaks only templates: _MAP,
    # _DEFAULT_BY_REGIME and apply_baselines can each return a template or
    # stand_down and nothing else. Leaving the built-ins selectable let the AI
    # half hand a channel something the deterministic half would never choose,
    # and the two then fought every cycle.
    #
    # It also silently changed position size. A built-in takes the global
    # Fixed Lot Size (strategy_lot_size, 0.1) via core_signal_resolution's
    # `if strategy_lot > 0 and not _is_template`, while a template uses its own
    # Anchor/Pending Lot fields. So an AI pick of "conservative_trial" over
    # "template:Auto Limit Balanced" quietly doubled the Reversal Engine from
    # the configured 0.05 to 0.1 -- observed live on tickets 1776668203/1776668211.
    # Sizing that swings on which strategy name a model happened to return is
    # not sizing the user configured.
    valid_strategies = _auto.auto_templates() + [_auto.STAND_DOWN]
    _auto_baselines = "\n".join(
        f"  {_c['source']}: {_auto.describe_cell(_c['source'], _auto_regime)}"
        for _c in channels_data
    ) or "  (no channels configured)"

    # Per-strategy split of each channel's record. The aggregate alone cannot
    # distinguish "this channel has no edge" from "this channel was run on the
    # wrong geometry for two days", and on 2026-08-16 that cost GOLD DIGGERS
    # INSTITUTIONAL a stand_down on WR=50%/PnL=-$1465 -- while the same 30 days
    # show it at 76.5% WR and +$228 on limit entries, with the whole loss
    # sitting in two wide-stop templates (Staged Ratchet 100-500: 13% WR,
    # -$1087; Asian Reversal - ATR: 33% WR, -$560). See
    # core_db_channel.get_channel_strategy_breakdown.
    try:
        _breakdown = _db.get_channel_strategy_breakdown()
    except Exception:
        _breakdown = {}

    # Today's realised state. The 30-day rows are the only thing this evaluator
    # has ever seen, so it re-ran every 15 minutes through 2026-08-17 (peak
    # +$348.76, closed -$88.48) and 08-18 selecting exactly as it would on a
    # good day -- it had no way to know the difference. A 30-day aggregate
    # cannot express "we are down $500 since this morning".
    try:
        from backend.src.services.risk.governor import day_pnl_and_peak
        _today_pnl, _today_peak = day_pnl_and_peak()
        _give_back = _today_peak - _today_pnl
        _bits = [f"  realised P&L today: ${_today_pnl:+.2f}"]
        if _today_peak > 0:
            _bits.append(f"  peak today: +${_today_peak:.2f}")
            if _give_back > 0:
                # Stated as "all of it, and $X beyond" once the day is red --
                # a bare percentage reads as 358% given back, which is true of
                # the ratio and useless as a description of the day.
                if _today_pnl < 0:
                    _bits.append(
                        f"  the whole peak has been given back, and ${abs(_today_pnl):.2f} beyond it"
                    )
                else:
                    _pct = _give_back / _today_peak * 100.0
                    _bits.append(
                        f"  given back from that peak: ${_give_back:.2f} ({_pct:.0f}% of it)"
                    )
        else:
            _bits.append("  the day has not been in profit at any point")
        _today_block = "\n".join(_bits)
    except Exception:
        _today_block = "  (unavailable)"

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
        for b in _breakdown.get(src, []):
            channel_lines.append(
                f'        under {b["strategy"]}: WR={b["win_rate"]:.1f}% '
                f'n={b["n"]} PnL=${b["net_pnl"]:.2f}'
            )

    # Describes the selectable set, which is valid_strategies -- not the
    # built-in STRATEGY_NAMES catalogue it used to print. Offering a menu
    # wider than what the validator accepts just invites answers that get
    # coerced back to the baseline, wasting the call and the reasoning.
    _TEMPLATE_SHAPES = {
        "template:Auto Limit Scalp":     "resting limit legs in the zone, SL35, banks 60% at 35 pips",
        "template:Auto Limit Balanced":  "resting limit legs, SL40, 40/80/130",
        "template:Auto Limit Trend":     "resting limit legs, SL50, runs to 300 pips",
        "template:Auto Market Balanced": "fills at market, SL40, 40/80/130",
        "template:Auto Market Trend":    "fills at market, SL50, runs to 300 pips",
        "template:Auto Market Runner":   "fills at market, SL60, 60/120/200",
    }
    strat_desc = "\n".join(
        f"  {s}: {_TEMPLATE_SHAPES.get(s, 'EA template')}"
        for s in valid_strategies if s != _auto.STAND_DOWN
    ) + f"\n  {_auto.STAND_DOWN}: do not trade this channel in these conditions"

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

AVAILABLE CHOICES (EA templates only, plus stand_down — nothing else is valid):
{strat_desc}

TODAY SO FAR:
{_today_block}

HOW TO USE TODAY'S STATE:
- The 30-day rows below say which geometry suits a channel. This block says what
  kind of day it is actually turning out to be, which the 30-day rows cannot.
- Down on the day, or well off the day's peak: prefer the tighter-stopped,
  nearer-target templates (Scalp over Balanced, Balanced over Trend), and
  stand_down a channel whose own split gives it no edge in these conditions.
  Trading a worse configuration harder is how a bad day compounds.
- Up on the day with the peak intact: no reason to change what is working.
- This is a tilt, not an override. A channel with a genuinely strong record
  under the geometry you are selecting does not become unsuitable because the
  day is red.

CHANNEL PERFORMANCE (last 30 days):
Each channel's headline row is followed by the same 30 days SPLIT BY the
strategy the trades actually ran under.
{chr(10).join(channel_lines) if channel_lines else "  No data yet"}

READING THE PERFORMANCE SPLIT (important):
- A channel's headline PnL mixes configurations, some of which are no longer
  in use. Judge the SIGNALS by the rows whose geometry resembles what you are
  considering, not by the headline.
- A loss concentrated in one or two mismatched configurations is evidence
  about THAT configuration, not evidence the channel lacks an edge. Do not
  stand a channel down on a headline loss when its split shows it profitable
  under geometry comparable to the template you would select.
- Conversely, a channel profitable only under a geometry you are NOT selecting
  is not evidence the selected one will work.

SELECTION RULES:
- Channels with open_trades > 0: keep the SAME choice as last_rec (no disruption mid-trade)
- Limit templates rest legs inside the signal's zone and win when price retraces to
  them: prefer them in ranging/weak conditions and when spread is wide.
- Market templates fill immediately and win when the move runs without a pullback:
  prefer them when ADX > 25 with clear directional structure, or when missing the
  entry costs more than a worse fill.
- Scalp (SL35) in the Asian session and when ATR is low; Trend/Runner (SL50-60) only
  when ATR and ADX both support a move of that size; Balanced (SL40) otherwise.
- Tighten toward Scalp when ATR > 35 (spike risk), RSI is extreme (>75 or <25), or the
  channel's rate under COMPARABLE geometry is below 55% -- read that from the split
  above, not the headline; a headline dragged down by a configuration you are not
  selecting does not trigger this.
- stand_down when no available template's geometry matches what the channel's own
  record supports in these conditions.

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
If a channel has no performance data, recommend "conservative" with confidence 0.4.

REGIME-TUNED EA TEMPLATES (backtested 31 days, per channel per market regime).
The current regime is "{_auto_regime}". These are the measured baselines --
prefer them unless the live conditions above genuinely argue otherwise, and
say why in the reasoning if you deviate:
{_auto_baselines}

Template shapes:
  template:Auto Limit Scalp     resting limit legs in the zone, SL35, banks 60% at 35 pips
  template:Auto Limit Balanced  resting limit legs, SL40, 40/80/130
  template:Auto Limit Trend     resting limit legs, SL50, runs to 300 pips
  template:Auto Market Balanced fills at market, SL40, 40/80/130
  template:Auto Market Trend    fills at market, SL50, runs to 300 pips
  template:Auto Market Runner   fills at market, SL60, 60/120/200

"{_auto.STAND_DOWN}" is a valid and expected answer: return it when a channel
has no edge in the current regime. Not trading is preferable to trading a
configuration with negative expectancy."""

    # ── Snapshot current recs before updating (for change detection) ─────────
    prev_recs: dict[str, str] = {
        ch["source"]: _db.get_channel_strategy_rec(ch["source"]).get("strategy", "")
        for ch in channels_data
    }

    # ── Call the configured AI provider ──────────────────────────────────────
    if not ai_provider.is_configured(cfg):
        log.info("channel_strategy_ai: no API key — using rule-based fallback")
        # The backtested cell, not rule_regime: rule_regime names a BUILT-IN
        # strategy, so writing it here dropped the channel out of template
        # management entirely (and onto the global fixed lot) every time the
        # API key was missing -- the one situation where falling back to the
        # measured baseline matters most. Same reason the unknown-strategy
        # branch below already uses baseline_for.
        for ch in channels_data:
            src       = ch["source"]
            pick      = _auto.baseline_for(src, _auto_regime)
            reasoning = (
                f"No API key — backtested {_auto_regime} baseline "
                f"({session_label}, ATR={atr_h1 or '?'}, ADX={adx_h1 or '?'})"
            )
            _db.set_channel_strategy_rec(src, pick, reasoning, 0.6)
            results[src] = {"strategy": pick, "reasoning": reasoning, "confidence": 0.6}
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
                # Fall back to the BACKTESTED cell for this channel/regime
                # rather than the H1 rule regime. An unparseable answer
                # should land on the measured baseline, which is the whole
                # point of having a deterministic floor -- rule_regime is a
                # built-in strategy pick and would quietly drop the channel
                # out of template management altogether.
                _fallback = _auto.baseline_for(src, _auto_regime)
                log.info("channel_strategy_ai: %s returned unknown strategy %r "
                         "-- using backtested baseline %s", src, strategy, _fallback)
                strategy = _fallback
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
        log.warning("channel_strategy_ai: AI call failed: %s — using backtested baseline", exc)
        # Baseline, not rule_regime -- see the no-API-key branch above. A
        # provider outage must not be able to silently move every channel off
        # its template (and onto the global fixed lot) until someone notices.
        for ch in channels_data:
            src       = ch["source"]
            pick      = _auto.baseline_for(src, _auto_regime)
            reasoning = f"AI unavailable ({exc.__class__.__name__}) — {_auto_regime} baseline"
            _db.set_channel_strategy_rec(src, pick, reasoning, 0.5)
            results[src] = {"strategy": pick, "reasoning": reasoning, "confidence": 0.5}

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
    from backend.src.db import database as _db
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
