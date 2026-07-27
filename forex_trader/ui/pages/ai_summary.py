"""
AI Market Analysis page — uses the AI provider configured in Settings > AI
(Claude or DeepSeek) to provide live gold market insights, news analysis,
technical commentary, and strategy recommendations.
"""

import json
from datetime import datetime, timezone
from typing import Callable, Optional

from nicegui import ui

import backend.src.config as cfg_module
from forex_trader.core import database as db_module
from backend.src.utils.models import STRATEGY_NAMES

def _uk_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _sentiment_style(sentiment: str) -> tuple[str, str]:
    """Returns (bg_class, icon) for a sentiment string."""
    s = (sentiment or "").lower()
    if s == "bullish":
        return "bg-green-900 border border-green-600", "trending_up"
    if s == "bearish":
        return "bg-red-900 border border-red-600", "trending_down"
    return "bg-gray-700 border border-gray-500", "trending_flat"


def _confidence_bar(confidence: float) -> None:
    """Render a simple confidence bar (0.0–1.0)."""
    pct = max(0, min(100, int(confidence * 100)))
    color = "#10b981" if pct >= 65 else "#f59e0b" if pct >= 40 else "#ef4444"
    with ui.row().classes("items-center gap-2 w-full"):
        ui.label(f"Confidence: {pct}%").classes("text-xs text-gray-400 w-24 shrink-0")
        with ui.element("div").classes("flex-1 bg-gray-700 rounded-full h-2"):
            ui.element("div").style(
                f"width:{pct}%;background:{color};height:8px;border-radius:9999px;"
            )


def render(get_engine: Callable):
    engine = get_engine()

    # ── State ──────────────────────────────────────────────────────────────────
    _result: list[Optional[dict]] = [None]
    _loading: list[bool] = [False]

    # ── Header ─────────────────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center justify-between px-4 pt-3 pb-1 flex-wrap gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.label("smart_toy").classes("material-icons text-yellow-400 text-xl")
            ui.label("AI Market Analysis").classes("text-lg font-bold text-yellow-300")
            ui.badge("XAUUSD Gold", color="amber").classes("text-xs")

        with ui.row().classes("items-center gap-3"):
            last_lbl = ui.label("Not yet researched").classes("text-xs text-gray-500")
            news_badge = ui.badge("", color="blue").classes("text-xs").style("display:none")
            research_btn = ui.button(
                "Research Now", icon="search"
            ).classes("bg-yellow-600 text-white text-sm px-4 py-1")

    ui.separator().classes("my-1")

    # ── Content area ───────────────────────────────────────────────────────────
    results_area = ui.column().classes("w-full px-4 pb-4 gap-4")

    def _render_placeholder():
        with results_area:
            with ui.card().classes("w-full bg-gray-800 rounded-lg p-8 text-center"):
                ui.label("smart_toy").classes("material-icons text-6xl text-gray-600 mb-3")
                ui.label("Click Research Now to get an AI-powered gold market analysis").classes(
                    "text-gray-400 text-sm"
                )
                ui.label(
                    "Analysis includes: market sentiment, price targets, key drivers, "
                    "technical summary, and strategy recommendation."
                ).classes("text-gray-600 text-xs mt-1")

    def _render_loading():
        results_area.clear()
        with results_area:
            with ui.card().classes("w-full bg-gray-800 rounded-lg p-8 text-center"):
                ui.spinner(size="lg", color="yellow")
                ui.label("Researching gold market conditions...").classes(
                    "text-gray-400 text-sm mt-3"
                )
                ui.label(
                    "AI is analysing price data, candles, signals and market news. "
                    "This may take up to 30 seconds."
                ).classes("text-gray-600 text-xs mt-1")

    def _render_results(data: dict):
        results_area.clear()
        with results_area:
            sentiment   = (data.get("sentiment") or "neutral").lower()
            confidence  = float(data.get("sentiment_confidence") or 0)
            today_bias  = data.get("today_bias") or ""
            price_low   = float(data.get("price_low") or 0)
            price_high  = float(data.get("price_high") or 0)
            summary     = data.get("summary") or ""
            tech_sum    = data.get("technical_summary") or ""
            drivers     = data.get("key_drivers") or []
            risk_factors = data.get("risk_factors") or []
            supports    = [float(x) for x in (data.get("support_levels") or []) if x]
            resistances = [float(x) for x in (data.get("resistance_levels") or []) if x]
            strat_rec   = data.get("strategy_recommendation") or "scale_out"
            strat_reason = data.get("strategy_reason") or ""
            signal_analysis = data.get("signal_analysis") or ""
            disclaimer  = data.get("disclaimer") or ""
            gen_at      = data.get("generated_at") or ""
            has_error   = bool(data.get("error"))

            # ── Row 1: Sentiment + Summary ──────────────────────────────────────
            with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):

                # Sentiment card
                sentiment_bg, sentiment_icon = _sentiment_style(sentiment)
                if sentiment == "bullish":
                    _fin_svg = (
                        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 64" '
                        'width="72" height="56" style="flex-shrink:0">'
                        # Candles
                        '<rect x="4"  y="40" width="8" height="16" fill="#4ade80"/>'
                        '<line x1="8"  y1="36" x2="8"  y2="40" stroke="#4ade80" stroke-width="2"/>'
                        '<line x1="8"  y1="56" x2="8"  y2="60" stroke="#4ade80" stroke-width="2"/>'
                        '<rect x="18" y="30" width="8" height="18" fill="#4ade80"/>'
                        '<line x1="22" y1="25" x2="22" y2="30" stroke="#4ade80" stroke-width="2"/>'
                        '<line x1="22" y1="48" x2="22" y2="52" stroke="#4ade80" stroke-width="2"/>'
                        '<rect x="32" y="18" width="8" height="20" fill="#4ade80"/>'
                        '<line x1="36" y1="13" x2="36" y2="18" stroke="#4ade80" stroke-width="2"/>'
                        '<line x1="36" y1="38" x2="36" y2="42" stroke="#4ade80" stroke-width="2"/>'
                        '<rect x="46" y="10" width="8" height="16" fill="#4ade80"/>'
                        '<line x1="50" y1="5"  x2="50" y2="10" stroke="#4ade80" stroke-width="2"/>'
                        '<line x1="50" y1="26" x2="50" y2="30" stroke="#4ade80" stroke-width="2"/>'
                        '<rect x="60" y="4"  width="8" height="14" fill="#4ade80"/>'
                        '<line x1="64" y1="1"  x2="64" y2="4"  stroke="#4ade80" stroke-width="2"/>'
                        '<line x1="64" y1="18" x2="64" y2="22" stroke="#4ade80" stroke-width="2"/>'
                        # Trend arrow
                        '<polyline points="6,54 20,44 34,32 48,22 62,14" '
                        'fill="none" stroke="#86efac" stroke-width="2" stroke-dasharray="3,2"/>'
                        '</svg>'
                    )
                elif sentiment == "bearish":
                    _fin_svg = (
                        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 64" '
                        'width="72" height="56" style="flex-shrink:0">'
                        '<rect x="4"  y="8"  width="8" height="14" fill="#f87171"/>'
                        '<line x1="8"  y1="4"  x2="8"  y2="8"  stroke="#f87171" stroke-width="2"/>'
                        '<line x1="8"  y1="22" x2="8"  y2="26" stroke="#f87171" stroke-width="2"/>'
                        '<rect x="18" y="18" width="8" height="16" fill="#f87171"/>'
                        '<line x1="22" y1="14" x2="22" y2="18" stroke="#f87171" stroke-width="2"/>'
                        '<line x1="22" y1="34" x2="22" y2="38" stroke="#f87171" stroke-width="2"/>'
                        '<rect x="32" y="26" width="8" height="18" fill="#f87171"/>'
                        '<line x1="36" y1="22" x2="36" y2="26" stroke="#f87171" stroke-width="2"/>'
                        '<line x1="36" y1="44" x2="36" y2="48" stroke="#f87171" stroke-width="2"/>'
                        '<rect x="46" y="34" width="8" height="20" fill="#f87171"/>'
                        '<line x1="50" y1="30" x2="50" y2="34" stroke="#f87171" stroke-width="2"/>'
                        '<line x1="50" y1="54" x2="50" y2="58" stroke="#f87171" stroke-width="2"/>'
                        '<rect x="60" y="42" width="8" height="18" fill="#f87171"/>'
                        '<line x1="64" y1="38" x2="64" y2="42" stroke="#f87171" stroke-width="2"/>'
                        '<line x1="64" y1="60" x2="64" y2="63" stroke="#f87171" stroke-width="2"/>'
                        '<polyline points="6,14 20,26 34,36 48,46 62,54" '
                        'fill="none" stroke="#fca5a5" stroke-width="2" stroke-dasharray="3,2"/>'
                        '</svg>'
                    )
                else:
                    _fin_svg = ""

                with ui.card().classes(
                    f"flex-1 min-w-56 rounded-lg p-0 overflow-hidden self-stretch {sentiment_bg}"
                ):
                    with ui.row().classes("p-4 gap-2 items-center justify-between w-full h-full"):
                        with ui.column().classes("gap-2 flex-1"):
                            ui.label("Market Sentiment").classes(
                                "text-xs text-gray-400 font-semibold tracking-wider uppercase"
                            )
                            with ui.row().classes("items-center gap-2"):
                                ui.label(sentiment_icon).classes(
                                    "material-icons text-3xl "
                                    + ("text-green-400" if sentiment == "bullish"
                                       else "text-red-400" if sentiment == "bearish"
                                       else "text-gray-400")
                                )
                                ui.label(sentiment.upper()).classes(
                                    "text-2xl font-bold "
                                    + ("text-green-400" if sentiment == "bullish"
                                       else "text-red-400" if sentiment == "bearish"
                                       else "text-gray-400")
                                )
                            _confidence_bar(confidence)
                            if today_bias:
                                ui.label(today_bias).classes("text-xs text-gray-300 mt-1 italic")
                        if _fin_svg:
                            ui.html(_fin_svg).classes("shrink-0 opacity-80")

                # Price target card
                if price_low > 0 or price_high > 0:
                    with ui.card().classes("flex-1 min-w-56 bg-gray-800 rounded-lg p-4 self-stretch"):
                        ui.label("Today's Price Target").classes(
                            "text-xs text-gray-400 font-semibold tracking-wider uppercase mb-2"
                        )
                        with ui.row().classes("items-center gap-3"):
                            with ui.column().classes("gap-0"):
                                ui.label("LOW").classes("text-xs text-gray-500")
                                ui.label(f"${price_low:,.2f}").classes(
                                    "text-xl font-bold font-mono text-red-300"
                                )
                            ui.label("—").classes("text-gray-500 text-lg")
                            with ui.column().classes("gap-0"):
                                ui.label("HIGH").classes("text-xs text-gray-500")
                                ui.label(f"${price_high:,.2f}").classes(
                                    "text-xl font-bold font-mono text-green-300"
                                )
                            if price_high > price_low:
                                rng = price_high - price_low
                                ui.label(f"Range: ${rng:.2f}").classes(
                                    "text-xs text-gray-500 self-end"
                                )

                        if supports or resistances:
                            ui.separator().classes("my-2")
                            with ui.grid(columns=2).classes("gap-2 w-full"):
                                if supports:
                                    with ui.column().classes("gap-1"):
                                        ui.label("Support").classes("text-xs text-gray-500")
                                        for s in supports[:3]:
                                            ui.label(f"${s:,.2f}").classes(
                                                "text-xs font-mono text-green-400"
                                            )
                                if resistances:
                                    with ui.column().classes("gap-1"):
                                        ui.label("Resistance").classes("text-xs text-gray-500")
                                        for r in resistances[:3]:
                                            ui.label(f"${r:,.2f}").classes(
                                                "text-xs font-mono text-red-400"
                                            )

            # ── Summary ─────────────────────────────────────────────────────────
            if summary:
                with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
                    ui.label("Executive Summary").classes(
                        "text-xs text-gray-400 font-semibold tracking-wider uppercase mb-2"
                    )
                    ui.label(summary).classes("text-sm text-gray-200 leading-relaxed")

            # ── Row 2: Drivers + Technical ──────────────────────────────────────
            with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):

                # Key drivers
                if drivers:
                    with ui.card().classes("flex-1 min-w-64 bg-gray-800 rounded-lg p-4 self-stretch"):
                        ui.label("What Could Move Gold Today").classes(
                            "text-xs text-gray-400 font-semibold tracking-wider uppercase mb-3"
                        )
                        for driver in drivers[:8]:
                            with ui.row().classes("w-full items-start gap-2 mb-1"):
                                ui.label("arrow_forward").classes(
                                    "material-icons text-yellow-500 text-sm shrink-0 mt-0.5"
                                )
                                ui.label(driver).classes("text-sm text-gray-300 flex-1")

                # Technical analysis
                if tech_sum:
                    with ui.card().classes("flex-1 min-w-64 bg-gray-800 rounded-lg p-4 self-stretch"):
                        ui.label("Technical Analysis").classes(
                            "text-xs text-gray-400 font-semibold tracking-wider uppercase mb-3"
                        )
                        ui.label(tech_sum).classes("text-sm text-gray-300 leading-relaxed")

                # Risk factors
                if risk_factors:
                    with ui.card().classes("flex-1 min-w-64 bg-gray-800 rounded-lg p-4 self-stretch"):
                        ui.label("Risk Factors").classes(
                            "text-xs text-gray-400 font-semibold tracking-wider uppercase mb-3"
                        )
                        for rf in risk_factors[:6]:
                            with ui.row().classes("w-full items-start gap-2 mb-1"):
                                ui.label("warning").classes(
                                    "material-icons text-orange-400 text-sm shrink-0 mt-0.5"
                                )
                                ui.label(rf).classes("text-sm text-gray-300 flex-1")

            # ── Strategy recommendation ─────────────────────────────────────────
            strat_name = STRATEGY_NAMES.get(strat_rec, strat_rec)
            with ui.card().classes("w-full rounded-lg p-0 overflow-hidden").style(
                "border:1px solid #d97706;"
            ):
                with ui.row().classes("w-full px-4 py-2 items-center gap-2").style(
                    "background:#1c1a0e;"
                ):
                    ui.label("emoji_objects").classes(
                        "material-icons text-yellow-400 text-lg"
                    )
                    ui.label("Recommended Strategy").classes(
                        "text-sm font-semibold text-yellow-300"
                    )
                    ui.badge(strat_name, color="amber").classes("text-xs ml-2")

                with ui.column().classes("px-4 py-3 gap-2 bg-gray-800 w-full"):
                    if strat_reason:
                        ui.label(strat_reason).classes("text-sm text-gray-200 leading-relaxed")

                    strategy_blurbs = {
                        "scale_out": (
                            "Scale Out closes 20% of the position at each TP level and moves "
                            "SL to breakeven after TP1. Ideal for controlled, gradual profit-taking."
                        ),
                        "be_runner": (
                            "BE Runner keeps the full position open and advances the SL to each "
                            "cleared TP level. Best for strong trending markets."
                        ),
                        "trail_stop": (
                            "Trailing Stop activates after TP1 and follows price with a configurable "
                            "distance. Captures extended moves in momentum conditions."
                        ),
                        "protected_scale": (
                            "Protected Scale holds the full position through TP1 and TP2 to lock "
                            "in breakeven protection, then scales 20% from TP3 onwards."
                        ),
                        "conservative": (
                            "Conservative uses fixed 5-pt SL and TP1 from the actual fill price — "
                            "signal levels are discarded after entry. 80% closes at TP1 (1:1 R:R), "
                            "SL moves to breakeven, and the 20% runner is managed with a 5-pt trailing stop. "
                            "Works with any signal quality."
                        ),
                        "no_sl_scale": (
                            "Trend Ratchet requires ADX > 30 at entry and uses a 1.5× emergency stop "
                            "instead of the signal SL. 20% closes at TP1 and TP3; SL ratchets to TP1 "
                            "at TP3 then steps two TP levels ahead from TP4+. Best for confirmed trending markets."
                        ),
                    }
                    blurb = strategy_blurbs.get(strat_rec, "")
                    if not blurb and strat_rec:
                        # Custom strategy — look up description from DB
                        try:
                            with db_module.db() as _conn:
                                _cs = _conn.execute(
                                    "SELECT name, description FROM custom_strategies WHERE id=?",
                                    (strat_rec,),
                                ).fetchone()
                                if _cs:
                                    blurb = (_cs[1] or "").split("\n")[0].strip().lstrip("#").strip()
                        except Exception:
                            pass
                    if blurb:
                        ui.label(blurb).classes("text-xs text-gray-400 italic")

            # ── Signal analysis ─────────────────────────────────────────────────
            if signal_analysis and signal_analysis != "—":
                with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
                    ui.label("Signal Analysis").classes(
                        "text-xs text-gray-400 font-semibold tracking-wider uppercase mb-2"
                    )
                    ui.label(signal_analysis).classes("text-sm text-gray-300")

            # ── Meta row ────────────────────────────────────────────────────────
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                meta_parts = []
                if gen_at:
                    try:
                        dt = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
                        local_dt = dt.astimezone()
                        meta_parts.append(f"Generated: {local_dt.strftime('%d %b %Y %H:%M')}")
                    except Exception:
                        meta_parts.append(f"Generated: {gen_at[:16]}")
                if data.get("fetched_news"):
                    meta_parts.append(f"{data.get('news_count', 0)} news headlines included")
                if has_error:
                    meta_parts.append("Analysis may be incomplete")
                ui.label("  |  ".join(meta_parts)).classes("text-xs text-gray-600")

                if disclaimer:
                    ui.label(disclaimer).classes("text-xs text-gray-600 italic")

    # ── Save research to DB ─────────────────────────────────────────────────────
    def _save_research(data: dict, gen_time: str) -> None:
        payload = json.dumps({"data": data, "saved_at": gen_time})
        db_module.set_app_config("ai_last_research", payload)

    # ── Load stored research on page open ───────────────────────────────────────
    def _load_stored_research() -> None:
        stored = db_module.get_app_config("ai_last_research")
        if not stored:
            _render_placeholder()
            return
        try:
            wrapper = json.loads(stored)
            data    = wrapper.get("data", {})
            saved   = wrapper.get("saved_at", "")
            _result[0] = data
            _render_results(data)
            if saved:
                try:
                    dt = datetime.fromisoformat(saved.replace("Z", "+00:00"))
                    local_dt = dt.astimezone()
                    last_lbl.text = f"Last research: {local_dt.strftime('%d %b %Y %H:%M')}"
                except Exception:
                    last_lbl.text = f"Last research: {saved[:16]}"
            if data.get("fetched_news"):
                news_badge.text = f"{data.get('news_count', 0)} news"
                news_badge.style("display:inline-flex")
        except Exception:
            _render_placeholder()

    async def do_research():
        if _loading[0]:
            return
        _loading[0] = True
        research_btn.disable()
        research_btn.text = "Researching..."
        last_lbl.text = "Fetching analysis..."
        _render_loading()

        try:
            import forex_trader.core.claude_ai as claude_ai
            config  = cfg_module.load()
            tick    = await engine.get_tick()
            candles = await engine.get_candles("M5", 50)

            # Recent signals (last 24h)
            import time as _time
            cutoff = _time.time() - 86400
            with db_module.db() as conn:
                recent_sigs = [
                    db_module.row_to_dict(r)
                    for r in conn.execute(
                        "SELECT * FROM vantage_tg_signals WHERE parsed_at>? "
                        "ORDER BY parsed_at DESC",
                        (cutoff,),
                    ).fetchall()
                ]

            perf = {}
            try:
                perf = await engine.compute_mt5_performance(90)
            except Exception:
                pass

            custom_strats = []
            try:
                with db_module.db() as conn:
                    custom_strats = [
                        db_module.row_to_dict(r)
                        for r in conn.execute(
                            "SELECT id, name, description FROM custom_strategies ORDER BY created_at"
                        ).fetchall()
                    ]
            except Exception:
                pass

            data = await claude_ai.request_market_analysis(
                tick=tick,
                candles=candles,
                recent_signals=recent_sigs,
                performance=perf,
                cfg=config,
                timeout=60,
                custom_strategies=custom_strats or None,
            )

            _result[0] = data
            _render_results(data)

            # Update last updated label
            gen_time = data.get("generated_at", datetime.now(timezone.utc).isoformat())
            last_lbl.text = f"Last updated: {_uk_time()} UK"
            _save_research(data, gen_time)

            # Show news badge
            if data.get("fetched_news"):
                news_badge.text = f"{data.get('news_count', 0)} news"
                news_badge.style("display:inline-flex")

        except Exception as e:
            results_area.clear()
            with results_area:
                with ui.card().classes("w-full bg-gray-800 rounded-lg p-6"):
                    ui.label(f"Analysis failed: {e}").classes("text-red-400 text-sm")
            last_lbl.text = "Error — try again"

        finally:
            _loading[0] = False
            research_btn.enable()
            research_btn.text = "Research Now"

    research_btn.on_click(do_research)

    # Load stored research (or show placeholder if none)
    _load_stored_research()
