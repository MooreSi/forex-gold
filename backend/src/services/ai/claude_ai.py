"""AI commentary wrapper — calls whichever provider is selected in Settings > AI
(Claude or DeepSeek) via core.ai_provider. Each function keeps its own prompt,
response schema, and fallback dict; only the "call the LLM" step is shared."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from backend.src.config import is_debug as _is_debug
from backend.src.services.ai import provider as ai_provider
from backend.src.services.ai import provider
from backend.src.services.positions import core_strategy_catalogue as strategy_catalogue

log = logging.getLogger(__name__)

_FALLBACK = {
    "summary": "Claude commentary unavailable.",
    "risk_rating": "unknown",
    "risk_score": 0.0,
    "setup_quality_score": 0.0,
    "risk_reward_comment": "",
    "entry_comment": "",
    "stop_loss_comment": "",
    "take_profit_comment": "",
    "market_context_comment": "",
    "would_do_differently": "",
    "recommended_user_review": [],
    "do_not_auto_trade_warning": "This commentary is for information only. Claude does not place trades.",
}

_SYSTEM = (
    "You are a professional gold (XAUUSD) trading risk analyst. "
    "Analyse the trade setup below and respond ONLY with a single minified JSON object "
    "matching this exact schema — no other text:\n"
    '{"commentary_type":"string","summary":"string","risk_rating":"low|medium|high|extreme",'
    '"risk_score":0.0,"setup_quality_score":0.0,"risk_reward_comment":"string",'
    '"entry_comment":"string","stop_loss_comment":"string","take_profit_comment":"string",'
    '"market_context_comment":"string","would_do_differently":"string",'
    '"recommended_user_review":["string"],'
    '"do_not_auto_trade_warning":"This commentary is for information only. Claude does not place trades."}'
)


async def request_commentary(
    event_type: str,
    trade: Optional[dict],
    signal: Optional[dict],
    tick,
    candles_m5: list[dict],
    cfg: dict,
    timeout: int = 45,
) -> dict:
    if not ai_provider.is_configured(cfg):
        return {**_FALLBACK, "commentary_type": event_type}

    obj = signal or trade or {}
    direction  = obj.get("direction", "?")
    entry_low  = obj.get("entry_low", 0)
    entry_high = obj.get("entry_high", 0)
    sl         = obj.get("stop_loss", 0)
    tps = [obj.get(f"tp{i}") for i in range(1, 6) if obj.get(f"tp{i}") is not None]

    lines = [
        f"Event: {event_type}",
        "Symbol: XAUUSD",
        f"Direction: {direction}",
        f"Entry range: {entry_low} - {entry_high}",
        f"Stop Loss: {sl}",
        f"TPs: {tps}",
    ]
    if tick:
        rr_sl  = abs((entry_low + entry_high) / 2 - sl)
        rr_tp  = abs((max(tps) if tps else 0) - (entry_low + entry_high) / 2)
        rr     = round(rr_tp / rr_sl, 2) if rr_sl > 0 else 0
        lines += [
            f"Current bid: {tick.bid}",
            f"Current ask: {tick.ask}",
            f"Spread: {tick.spread_points} points",
            f"Risk/Reward (to TP{len(tps)}): 1:{rr}",
        ]
    if trade and event_type in ("trade_close", "manual_close"):
        lines += [
            f"Entry price: {trade.get('entry_price')}",
            f"Net PnL: {trade.get('net_pnl')}",
            f"Exit reason: {trade.get('exit_reason')}",
        ]
    if candles_m5:
        last5 = candles_m5[-5:]
        lines.append("Recent M5 closes: " + ", ".join(str(c["close"]) for c in last5))

    prompt = "\n".join(lines)

    try:
        raw = await ai_provider.complete(cfg, _SYSTEM, prompt, max_tokens=800, timeout=timeout)
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:])
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        data = json.loads(raw)
        data["commentary_type"] = event_type
        return data
    except ai_provider.TruncatedResponseError:
        log.debug("AI commentary truncated (max_tokens); using fallback")
    except asyncio.TimeoutError:
        log.warning("AI commentary timeout")
    except Exception as e:
        log.warning("AI commentary error: %s: %s", type(e).__name__, e)

    return {**_FALLBACK, "commentary_type": event_type}


# ── Market analysis ───────────────────────────────────────────────────────────

_MARKET_SYSTEM = (
    "You are a professional gold (XAUUSD) market analyst with deep knowledge of "
    "macroeconomics, technical analysis, and gold market drivers. "
    "Analyse the current market conditions and respond ONLY with a single minified JSON object "
    "matching this exact schema — no other text, no markdown fences:\n"
    '{"sentiment":"bullish|bearish|neutral","sentiment_confidence":0.0,"today_bias":"string",'
    '"price_low":0.0,"price_high":0.0,'
    '"key_drivers":["string"],"technical_summary":"string",'
    '"support_levels":[0.0],"resistance_levels":[0.0],'
    '"strategy_recommendation":"<exact key from the Available strategies list in the user message>",'
    '"strategy_reason":"string","risk_factors":["string"],'
    '"signal_analysis":"string","summary":"string",'
    '"disclaimer":"AI analysis for informational purposes only. Not financial advice."}'
)


async def _fetch_gold_news() -> list[str]:
    """Fetch recent gold news headlines from Yahoo Finance RSS (no API key required)."""
    if _is_debug():
        return [
            "[debug] Gold steadies near 2400 as traders await canned FOMC event",
            "[debug] Simulated headline: dollar softens, bullion bid",
        ]
    headlines: list[str] = []
    try:
        import httpx, re  # noqa: F401
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://feeds.finance.yahoo.com/rss/2.0/headline"
                "?s=GC%3DF&region=US&lang=en-US",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                titles = re.findall(r"<title><!\[CDATA\[(.+?)\]\]></title>", r.text)
                if not titles:
                    all_titles = re.findall(r"<title>(.+?)</title>", r.text)
                    titles = [t for t in all_titles[1:] if "Yahoo" not in t][:10]
                headlines.extend(t.strip() for t in titles[:10] if t.strip())
    except Exception:
        pass
    return headlines


async def request_market_analysis(
    tick,
    candles: list[dict],
    recent_signals: list[dict],
    performance: dict,
    cfg: dict,
    timeout: int = 60,
    custom_strategies: list[dict] | None = None,
    strategies: list[dict] | None = None,
) -> dict:
    """Request a full market analysis from the configured AI provider for the current gold market.

    `strategies` is a core_strategy_catalogue entry list — every pre-coded
    strategy, custom strategy and EA template the user currently has, each
    with a summary of what it does. It is built fresh by the caller so a
    template created moments ago is recommendable straight away. When it is
    omitted the catalogue is built here, so no caller can accidentally fall
    back to a stale hardcoded subset; `custom_strategies` is kept only for
    older callers and is ignored once a catalogue is present.
    """
    if not ai_provider.is_configured(cfg):
        return {
            "sentiment": "neutral",
            "sentiment_confidence": 0.0,
            "today_bias": "AI provider not configured",
            "price_low": 0.0,
            "price_high": 0.0,
            "key_drivers": ["Configure an API key in Settings > AI to enable analysis"],
            "technical_summary": "API key required",
            "support_levels": [],
            "resistance_levels": [],
            "strategy_recommendation": "scale_out",
            "strategy_reason": "Default — configure API key",
            "risk_factors": [],
            "signal_analysis": "—",
            "summary": "Configure an API key in Settings > AI to enable AI market analysis.",
            "disclaimer": "AI analysis for informational purposes only. Not financial advice.",
            "error": "no_api_key",
        }

    news = await _fetch_gold_news()

    lines = [
        f"Date/Time: {datetime.now(timezone.utc).strftime('%A %d %B %Y %H:%M UTC')}",
        "Symbol: XAUUSD (Gold vs US Dollar)",
        "",
    ]

    if tick:
        lines += [
            f"Current Bid: {tick.bid}",
            f"Current Ask: {tick.ask}",
            f"Spread: {tick.spread_points or 0:.0f} points",
        ]

    if candles:
        closes = [c["close"] for c in candles[-50:] if c.get("close")]
        highs  = [c["high"]  for c in candles[-50:] if c.get("high")]
        lows   = [c["low"]   for c in candles[-50:] if c.get("low")]
        if closes:
            lines += [
                "",
                f"Recent M5 closes (last 20): {', '.join(f'{v:.2f}' for v in closes[-20:])}",
                f"Session high: {max(highs):.2f}  Session low: {min(lows):.2f}",
                f"Overall direction: {'UP' if closes[-1] > closes[0] else 'DOWN'} "
                f"({closes[-1] - closes[0]:+.2f} over {len(closes)} bars)",
            ]

    if recent_signals:
        lines.append("")
        lines.append(f"Signals detected in last 24h: {len(recent_signals)}")
        buys  = sum(1 for s in recent_signals if s.get("direction") == "BUY")
        sells = sum(1 for s in recent_signals if s.get("direction") == "SELL")
        lines.append(f"  BUY signals: {buys}  SELL signals: {sells}")
        for s in recent_signals[:4]:
            lines.append(
                f"  {s.get('direction','?')} entry {s.get('entry_low') or 0:.0f}-"
                f"{s.get('entry_high') or 0:.0f} SL {s.get('stop_loss') or 0:.0f} "
                f"TP1 {s.get('tp1') or 0:.0f}"
            )

    if performance:
        lines += [
            "",
            f"Account balance: ${performance.get('balance') or 0:,.2f}",
            f"Win rate (90d): {performance.get('win_rate_pct') or 0:.1f}%",
            f"Profit factor: {performance.get('profit_factor') or 0:.2f}",
            f"Open positions: {performance.get('open_trades', 0)}",
        ]

    # Every option the user actually has — built-ins, custom strategies and
    # EA templates alike — rather than the six built-ins this prompt used to
    # hardcode. Templates are described from their live field values, which
    # is what lets the model reason about one the user created after this
    # code was written.
    if strategies is None:
        try:
            strategies = strategy_catalogue.build_catalogue()
        except Exception as exc:
            log.warning("Market analysis: strategy catalogue unavailable: %s", exc)
            strategies = []
    if strategies:
        lines.append("")
        lines += strategy_catalogue.prompt_lines(strategies)
    elif custom_strategies:
        # Legacy caller with no catalogue and no working DB access.
        lines += ["", "Available strategies (use the exact key in strategy_recommendation):"]
        for cs in custom_strategies:
            cid   = cs.get("id", "")
            cname = cs.get("name", cid)
            cdesc = (cs.get("description") or "").split("\n")[0].strip().lstrip("#").strip()
            lines.append(f"  {cid}: {cname} — {cdesc[:120]}")

    if news:
        lines += ["", "Recent news headlines related to gold/USD:"]
        for h in news[:8]:
            lines.append(f"  - {h}")

    lines += [
        "",
        "Task: Provide a complete gold market analysis covering today's likely price range, "
        "key fundamental and technical drivers, support/resistance levels, what could move "
        "price today (macro events, USD strength, geopolitics, risk sentiment), whether the "
        "market is bullish or bearish, and which single option from the Available strategies "
        "list above best suits current conditions. Consider every option — built-in strategies, "
        "custom strategies and EA templates are all equally eligible; judge each on the "
        "behaviour described, not on its name. Put its exact key in strategy_recommendation "
        "and say in strategy_reason why it beats the alternatives today.",
    ]

    prompt = "\n".join(lines)

    try:
        raw = await ai_provider.complete(cfg, _MARKET_SYSTEM, prompt, max_tokens=2000, timeout=timeout)
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:])
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        data = json.loads(raw)
        # The model is free-typing a key, and templates arrive named rather
        # than keyed often enough to be worth normalising ("Sniper" ->
        # "template:Sniper"). A key that matches nothing at all is dropped to
        # scale_out rather than shown as a strategy the user does not have.
        if strategies:
            _rec = strategy_catalogue.resolve_key(data.get("strategy_recommendation"), strategies)
            if _rec is None:
                # scale_out unless the user has hidden it, in which case the
                # first thing they do have beats naming something they do not.
                _keys = strategy_catalogue.valid_keys(strategies)
                _rec = "scale_out" if "scale_out" in _keys else _keys[0]
                log.info(
                    "Market analysis: unrecognised strategy_recommendation %r — using %s",
                    data.get("strategy_recommendation"), _rec,
                )
            data["strategy_recommendation"] = _rec
            # Resolved here rather than at render time so a stored analysis
            # still displays the template/strategy it actually meant, even
            # after that entry is renamed or deleted.
            _label, _summary = strategy_catalogue.describe(_rec, strategies)
            data["strategy_label"]   = _label
            data["strategy_summary"] = _summary
        data["fetched_news"]  = bool(news)
        data["news_count"]    = len(news)
        data["generated_at"]  = datetime.now(timezone.utc).isoformat()
        return data
    except asyncio.TimeoutError:
        log.warning("Market analysis timeout")
    except Exception as e:
        log.warning("Market analysis error: %s: %s", type(e).__name__, e)

    return {
        "sentiment": "neutral",
        "sentiment_confidence": 0.0,
        "today_bias": "Analysis failed",
        "price_low": 0.0,
        "price_high": 0.0,
        "key_drivers": [],
        "technical_summary": "Unable to generate analysis at this time.",
        "support_levels": [],
        "resistance_levels": [],
        "strategy_recommendation": "scale_out",
        "strategy_reason": "Default fallback — retry research.",
        "risk_factors": [],
        "signal_analysis": "—",
        "summary": "Analysis failed. Check API key and connectivity, then click Research Now.",
        "disclaimer": "AI analysis for informational purposes only. Not financial advice.",
        "error": "analysis_failed",
    }


_CLASSIFY_SYSTEM = (
    "You are a Telegram signal channel analyst. "
    "A message arrived from a gold (XAUUSD) trading signal channel but the app could not parse it. "
    "Classify the message and respond ONLY with a single minified JSON object matching the exact schema below. "
    "No other text.\n"
    '{"is_signal":false,"signal_type":"full_signal|partial_signal|update|announcement|off_topic|other",'
    '"suggested_action":"use_format_ab|use_gd2|ignore|review_manually",'
    '"suggested_format":"format_ab|gd2|null",'
    '"confidence":0.0,"summary":"one sentence description","reasoning":"brief explanation"}'
)


async def classify_unknown_message(
    text: str,
    channel_name: str,
    cfg: dict,
    timeout: int = 30,
) -> dict:
    """Classify an unrecognised Telegram message using the configured AI provider."""
    _fallback = {
        "is_signal": False,
        "signal_type": "unknown",
        "suggested_action": "review_manually",
        "suggested_format": None,
        "confidence": 0.0,
        "summary": "AI unavailable — manual review required",
        "reasoning": "",
    }
    if not ai_provider.is_configured(cfg):
        return _fallback

    prompt = (
        f"Channel: {channel_name}\n\n"
        f"Message:\n{text[:1500]}"
    )
    try:
        import re as _re
        raw = await ai_provider.complete(cfg, _CLASSIFY_SYSTEM, prompt, max_tokens=400, timeout=timeout)
        m = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if m:
            return json.loads(m.group(0))
        return {**_fallback, "summary": raw}
    except Exception as e:
        log.warning("classify_unknown_message error: %s: %s", type(e).__name__, e)
        return {**_fallback, "summary": f"Analysis failed: {e}"}


# ── Daily email performance analysis ─────────────────────────────────────────

_DAILY_SYSTEM = (
    "You are a professional XAUUSD trading coach reviewing a trader's daily performance. "
    "Your role is to analyse each trade objectively, identify what could have been done better, "
    "recommend which strategy would have improved outcomes, and give concise actionable "
    "takeaways for the next trading day. "
    "XAUUSD context: 1 pt = $0.01 price move, 1 standard lot = 100 oz, "
    "$1 price move = $100/lot. Strategies available: scale_out, be_runner, trail_stop, "
    "protected_scale, conservative, conservative_trial, no_sl_scale. "
    "Respond ONLY with a single minified JSON object matching this exact schema — no other text:\n"
    '{"overall_summary":"string",'
    '"overall_pnl_verdict":"positive|negative|breakeven",'
    '"best_strategy_today":"string (strategy id)",'
    '"best_strategy_reason":"string",'
    '"key_lesson":"string (single most important takeaway for tomorrow)",'
    '"tomorrow_focus":["string","string","string"],'
    '"trade_analysis":[{"trade_id":"string","verdict":"good|ok|poor",'
    '"max_tp_reached":"string (e.g. TP2 or SL)",'
    '"left_on_table":"string (what extra profit was available)",'
    '"better_strategy":"string (strategy id that would have done better, or same)",'
    '"better_strategy_pnl_est":"string (rough $ estimate of improvement)",'
    '"note":"string (1-2 sentence coaching note)"}]}'
)


async def generate_daily_analysis(
    closed_trades: list[dict],
    balance: float,
    daily_pnl: float,
    cfg: dict,
    timeout: int = 60,
) -> dict:
    """Generate the AI coaching analysis of the day's trades for the daily email."""
    _fallback = {
        "overall_summary": "AI analysis unavailable — check API key in Settings > AI.",
        "overall_pnl_verdict": "unknown",
        "best_strategy_today": "",
        "best_strategy_reason": "",
        "key_lesson": "",
        "tomorrow_focus": [],
        "trade_analysis": [],
    }
    if not ai_provider.is_configured(cfg):
        return _fallback
    if not closed_trades:
        return {**_fallback, "overall_summary": "No trades closed today — nothing to analyse."}

    import re as _re

    def _tp_label(t: dict) -> str:
        """Return the highest TP that was set and whose level the close price reached."""
        cp   = float(t.get("close_price") or 0)
        ep   = float(t.get("entry_price") or 0)
        is_b = (t.get("direction", "").upper() == "BUY")
        highest = "SL / early exit"
        for i in range(1, 9):
            tp = t.get(f"tp{i}")
            if tp is None:
                break
            tp = float(tp)
            if (is_b and cp >= tp) or (not is_b and cp <= tp):
                highest = f"TP{i} ({tp:.2f})"
        return highest

    def _trade_summary(t: dict) -> str:
        lots  = float(t.get("lot_size") or 0)
        pnl   = float(t.get("mt5_profit") or t.get("net_pnl") or 0)
        sl    = t.get("stop_loss")
        tps   = [f"TP{i}={t[f'tp{i}']:.2f}" for i in range(1, 9) if t.get(f"tp{i}") is not None]
        return (
            f"trade_id={t.get('trade_id','?')} | {t.get('direction','?')} "
            f"@ entry={float(t.get('entry_price',0)):.2f} "
            f"close={float(t.get('close_price',0)):.2f} "
            f"lots={lots:.2f} SL={float(sl):.2f if sl else 'none'} "
            f"{' '.join(tps) if tps else 'no TPs set'} "
            f"exit={t.get('exit_reason','?')} strategy={t.get('strategy','?')} "
            f"pnl=${pnl:+.2f} max_tp_reached={_tp_label(t)}"
        )

    trade_lines = "\n".join(f"  {_trade_summary(t)}" for t in closed_trades)
    user_msg = (
        f"Trading day summary — account balance ${balance:,.2f}, "
        f"day P&L ${daily_pnl:+.2f}\n\n"
        f"Closed trades today ({len(closed_trades)}):\n{trade_lines}\n\n"
        "Analyse each trade and the day as a whole. "
        "For max_tp_reached, use the highest TP level whose price the close_price reached or passed. "
        "For left_on_table, estimate what additional P&L was available if the runner had been held "
        "to a higher TP (or if a losing trade had been exited earlier). "
        "For better_strategy, only suggest an alternative if it would materially improve the outcome."
    )

    try:
        raw = await ai_provider.complete(cfg, _DAILY_SYSTEM, user_msg, max_tokens=1800, timeout=timeout)
        m = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if m:
            return json.loads(m.group(0))
        return {**_fallback, "overall_summary": raw}
    except Exception as e:
        log.warning("generate_daily_analysis error: %s: %s", type(e).__name__, e)
        return {**_fallback, "overall_summary": f"Analysis failed: {e}"}
