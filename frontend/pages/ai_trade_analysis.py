"""
AI Trade Analysis — per-channel signal quality vs actual trade outcome.

For each Telegram channel: detects phantom TPs, analyses SL management,
models partial-close alternatives, flags bad R:R, maps session bias,
detects consecutive loss clusters, and recommends lot sizing.
"""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from nicegui import ui

from backend.src.controllers import settings_controller as cfg_module
from backend.src.services.ai import claude_ai as ai_module
from backend.src.services.ai import provider as ai_provider
from backend.src.controllers import ai_analysis_controller as ai_ctl

# ── Regex for channel update message scanning ─────────────────────────────────
_TP_HIT_RE = re.compile(
    r'TP\s*(\d+)\s*(hit|hitt|done|reached|closed|filled|✅|🎯|🤑)',
    re.IGNORECASE,
)
_SL_HIT_RE = re.compile(
    r'(stop\s*loss|sl|stopped)\s*(hit|hitt|done|reached|triggered)',
    re.IGNORECASE,
)


# ── Session helper ─────────────────────────────────────────────────────────────

def _session_from_ts(ts: Optional[float]) -> str:
    if not ts:
        return "Unknown"
    hour = datetime.fromtimestamp(float(ts), tz=timezone.utc).hour
    if   0  <= hour < 7:  return "Asian"
    elif 7  <= hour < 12: return "London"
    elif 12 <= hour < 16: return "London/NY"
    elif 16 <= hour < 21: return "NY"
    else:                  return "Off-hours"


def _fmt_ts(ts: Optional[float]) -> str:
    """Format an MT5 broker timestamp. UTC display matches broker server time (UTC+3)."""
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ── Data gathering ─────────────────────────────────────────────────────────────

def _gather_channel_data(db_path: str, days: int) -> list[dict]:
    """Moved verbatim to analytics/ai_analysis_repo (M3 page drain)."""
    return ai_ctl.gather_channel_data(db_path, days)


# ── Strategy vs DPM data gathering ─────────────────────────────────────────────

def _gather_strategy_dpm_data(db_path: str, days: int) -> dict:
    """Moved verbatim to analytics/ai_analysis_repo (M3 page drain)."""
    return ai_ctl.gather_strategy_dpm_data(db_path, days)


def _build_prompt(ch: dict, days: int) -> str:
    stats = ch["stats"]
    lines = [
        f"CHANNEL: {ch['channel_name']}",
        f"ANALYSIS PERIOD: Last {days} day(s)",
        f"",
        f"SUMMARY STATS:",
        f"  Total signals parsed: {stats['total_signals']}",
        f"  Signals with linked trades: {stats['signals_with_trades']}",
        f"  Closed trades: {stats['closed_trades']}",
        f"  Wins (net positive P&L): {stats['wins']}",
        f"  SL hits: {stats['sl_hits']}",
        f"  Win rate: {stats['win_rate_pct']}%",
        f"  Total net P&L: ${float(stats['total_pnl'] or 0):+.2f}",
        f"  Phantom TP detections (Python pre-scan): {stats['phantom_tp_count']}",
        f"  Max consecutive losses: {stats['max_consecutive_losses']}",
        f"  Channel posts breakeven/SL-management instructions: {stats['be_instructions_in_channel']}",
        f"  Actual P&L vs simulated 50%-at-TP1 model: actual=${float(stats['actual_pnl_sum'] or 0):+.2f} | simulated=${float(stats['simulated_50pct_pnl_sum'] or 0):+.2f}",
        f"  Session P&L breakdown: {stats['session_pnl']}",
        f"",
        f"SIGNAL-BY-SIGNAL RECORDS ({len(ch['signals'])} signals):",
    ]

    for i, s in enumerate(ch["signals"], 1):
        tps_str = " | ".join(f"TP{j+1}={float(v or 0):.1f}" for j, v in enumerate(s["tps"])) if s["tps"] else "none"
        rr_str  = f"TP1 R:R={s['rr_tp1']} | Final R:R={s['rr_last']}" if s["rr_tp1"] else "R:R unknown"
        drift   = f"drift={s['entry_drift_pips']}pips" if s["entry_drift_pips"] is not None else "drift=N/A"

        lines += [
            f"",
            f"--- [{i}] {_fmt_ts(s.get('parsed_at'))} UTC | {s['session']} session ---",
            f"  Signal: {s['direction']} | Entry {s['entry_low']}-{s['entry_high']} | SL {s['stop_loss']} | {tps_str}",
            f"  {rr_str}",
        ]

        # Channel update chain (truncated to key info)
        if s["reply_chain"]:
            chain_parts = [f'"{rr["text"][:80]}"' for rr in s["reply_chain"][:8]]
            lines.append(f"  Channel updates: {' → '.join(chain_parts)}")
        else:
            lines.append("  Channel updates: none")

        claimed = (f"TP{n}" for n in s["claimed_tps"])
        claimed_str = ", ".join(claimed) if s["claimed_tps"] else "none"
        lines.append(
            f"  Channel claimed: TPs hit = [{claimed_str}] | SL acknowledged = {s['claimed_sl']}"
        )

        t = s["trade"]
        if t:
            be = "YES" if t["sl_moved_to_be"] else "no"
            pcs = (f"{len(t['partial_closes'])} partial close(s): " +
                   ", ".join(f"{p['reason']}@{p['close_price']}" for p in t["partial_closes"][:4])
                   ) if t["partial_closes"] else "no partial closes"
            lines += [
                f"  Actual trade: entry={t['entry_price']} | close={t['close_price']} "
                f"| exit={t['exit_reason']} | P&L=${float(t['net_pnl'] or 0):+.2f} | SL→BE={be} | {drift}",
                f"  {pcs}",
            ]
            if s["simulated_50pct_tp1_pnl"] is not None:
                lines.append(
                    f"  Simulated 50%-at-TP1 outcome: ${s['simulated_50pct_tp1_pnl']:+.2f} "
                    f"(actual: ${float(t['net_pnl'] or 0):+.2f})"
                )
            if s["is_phantom"]:
                lines.append(
                    f"  *** PHANTOM TP: channel claimed TP{s['claimed_tps']} hit "
                    f"but trade exited at {t['exit_reason']} with ${float(t['net_pnl'] or 0):+.2f} ***"
                )
        else:
            lines.append("  Trade record: not available (signal may not have been executed)")

    lines += [
        "",
        "TASK: Analyse this channel's signal quality and produce your JSON response.",
    ]
    return "\n".join(lines)


# ── Claude response schema ──────────────────────────────────────────────────────

_ANALYSIS_SCHEMA = (
    '{"reliability_score":0,'
    '"reliability_label":"string",'
    '"executive_summary":"string",'
    '"phantom_tps":[{"date":"string","direction":"string","claimed":"string","actual":"string","pnl":0.0}],'
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


# ── Claude API call ────────────────────────────────────────────────────────────

_MAX_SIGNALS_IN_PROMPT = 60   # cap to avoid hitting token/timeout limits


async def run_channel_analysis(
    channel_data: dict,
    days: int,
    cfg: dict,
    timeout: int = 150,
) -> dict:
    """Call the configured AI provider to analyse a single channel's signal quality."""
    if not ai_provider.is_configured(cfg):
        return {"error": "AI provider not configured. Set it in Settings > AI."}

    try:
        # Cap signal count to avoid oversized prompts on busy channels
        capped = dict(channel_data)
        if len(capped.get("signals", [])) > _MAX_SIGNALS_IN_PROMPT:
            capped = {**capped, "signals": capped["signals"][:_MAX_SIGNALS_IN_PROMPT]}
        prompt = _build_prompt(capped, days)
        raw = await ai_provider.complete(cfg, _ANALYSIS_SYSTEM, prompt, max_tokens=8192, timeout=timeout)
        # Strip accidental markdown fences
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except asyncio.TimeoutError:
        return {"error": "Analysis timed out — the prompt may be too large. Try a shorter time window."}
    except json.JSONDecodeError as e:
        return {"error": f"AI returned invalid JSON: {e}"}
    except Exception as e:
        return {"error": f"Analysis failed: {e}"}


# ── Strategy vs DPM prompt / schema / system ───────────────────────────────────

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


def _build_strategy_dpm_prompt(data: dict) -> str:
    lines = [
        "STRATEGY vs DPM PERFORMANCE COMPARISON",
        f"Period: Last {data['days']} day(s)  |  Total closed trades: {data['total_closed']}",
        "",
        "HEAD-TO-HEAD:",
    ]

    def _fmt(label: str, s: dict) -> list[str]:
        if s["count"] == 0:
            return [f"  {label}: no trades in period"]
        return [
            f"  {label}:",
            f"    Trades: {s['count']}  Wins: {s['wins']}  Win rate: {s['win_rate']}%  "
            f"Total P&L: ${s['total_pnl']:+.2f}  Avg P&L: ${s['avg_pnl']:+.2f}  "
            f"Profit factor: {s['profit_factor']}  Avg hold: {s['avg_hold_min']:.0f} min",
            f"    SL exits: {s['sl_exits']}  BE exits: {s['be_exits']}",
        ]

    lines += _fmt("DPM-managed trades", data["dpm_stats"])
    lines += _fmt("Fixed-strategy trades (no DPM)", data["fixed_stats"])

    if data["strategy_breakdown"]:
        lines += ["", "FIXED STRATEGY BREAKDOWN (non-DPM trades only):"]
        for s in data["strategy_breakdown"]:
            lines.append(
                f"  {s['strategy']}: {s['count']} trades | {s['win_rate']}% WR | "
                f"${s['total_pnl']:+.2f} total | PF {s['profit_factor']} | "
                f"avg hold {s['avg_hold_min']:.0f} min"
            )
    else:
        lines.append("\n  (no fixed-strategy trades in this period)")

    d = data["dpm_detail"]
    if d["count"] > 0:
        lines += [
            "",
            f"DPM DETAIL ({d['count']} trades, avg R-multiple: {d['avg_r_multiple']}):",
        ]
        if d["avg_trail_capture"] is not None:
            lines.append(
                f"  Avg trail capture ratio: {d['avg_trail_capture']:.0%} "
                f"(final_pnl / peak_pnl on trail exits — higher is better)"
            )
        lines.append(
            f"  Calibrated params: {d['calibrated_trades']} trades  |  "
            f"Uncalibrated: {d['uncalibrated_trades']} trades"
        )
        if d["regime_breakdown"]:
            lines.append("  Regime performance:")
            for reg, rs in sorted(d["regime_breakdown"].items()):
                wr = round(rs["wins"] / rs["count"] * 100) if rs["count"] else 0
                lines.append(
                    f"    {reg}: {rs['count']} trades  ${rs['pnl']:+.2f}  {wr}% WR"
                )
        if d["session_breakdown"]:
            lines.append("  Session performance:")
            for sess, rs in sorted(d["session_breakdown"].items()):
                wr = round(rs["wins"] / rs["count"] * 100) if rs["count"] else 0
                lines.append(
                    f"    {sess}: {rs['count']} trades  ${rs['pnl']:+.2f}  {wr}% WR"
                )
        if d["exit_breakdown"]:
            lines.append("  Exit type breakdown:")
            for et, rs in sorted(d["exit_breakdown"].items()):
                lines.append(f"    {et}: {rs['count']} exits  ${rs['pnl']:+.2f}")
    else:
        lines.append("\nNo DPM performance records found in this period.")

    lines += [
        "",
        "TASK: Determine whether DPM or fixed strategies are performing better overall, "
        "and give a concrete recommendation on which approach to use going forward.",
    ]
    return "\n".join(lines)


async def run_strategy_dpm_analysis(
    data: dict,
    cfg: dict,
    timeout: int = 60,
) -> dict:
    """Call the configured AI provider to analyse strategy vs DPM performance."""
    if not ai_provider.is_configured(cfg):
        return {"error": "AI provider not configured."}

    try:
        prompt = _build_strategy_dpm_prompt(data)
        raw = await ai_provider.complete(cfg, _STRATEGY_DPM_SYSTEM, prompt, max_tokens=1024, timeout=timeout)
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except asyncio.TimeoutError:
        return {"error": "Analysis timed out."}
    except json.JSONDecodeError as e:
        return {"error": f"AI returned invalid JSON: {e}"}
    except Exception as e:
        return {"error": f"Analysis failed: {e}"}


# ── Signal Generator analysis ─────────────────────────────────────────────────

def _gather_signal_generator_data(db_path: str, days: int) -> dict:
    """Moved verbatim to analytics/ai_analysis_repo (M3 page drain)."""
    return ai_ctl.gather_signal_generator_data(db_path, days)


def _build_signal_generator_prompt(data: dict) -> str:
    lines = [
        f"SIGNAL GENERATOR ANALYSIS — Last {data['days']} day(s)",
        f"Total internal engine trades in period: {data['total_trades']}",
        "",
    ]
    for eng in data["engines"]:
        a = eng["all"]
        e = eng["early_half"]
        l = eng["late_half"]
        lines += [
            f"ENGINE: {eng['label']} (strategy={eng['strategy']})",
            f"  FULL PERIOD: {a['count']} trades | {a['win_rate']}% WR | ${a['total_pnl']:+.2f} P&L | "
            f"avg ${a['avg_pnl']:+.2f} | SL exits: {a['sl_exits']} | BE exits: {a['be_exits']}",
            f"  FIRST HALF:  {e['count']} trades | {e['win_rate']}% WR | ${e['total_pnl']:+.2f} P&L",
            f"  SECOND HALF: {l['count']} trades | {l['win_rate']}% WR | ${l['total_pnl']:+.2f} P&L",
            f"  TREND: {'Improving' if (l['win_rate'] > e['win_rate'] and l['count'] >= 3 and e['count'] >= 3) else 'Declining' if (l['win_rate'] < e['win_rate'] and l['count'] >= 3 and e['count'] >= 3) else 'Insufficient data'}",
            "",
        ]
    lines += [
        "TASK: Assess each engine's development, ML contribution, and whether they are "
        "beginning to act like professional traders. Give a collective verdict on the "
        "signal generator suite as a whole."
    ]
    return "\n".join(lines)


async def run_signal_generator_analysis(
    data: dict,
    cfg: dict,
    timeout: int = 60,
) -> dict:
    if not ai_provider.is_configured(cfg):
        return {"error": "AI provider not configured."}
    try:
        prompt = _build_signal_generator_prompt(data)
        raw = await ai_provider.complete(cfg, _SIGNAL_GEN_SYSTEM, prompt, max_tokens=2048, timeout=timeout)
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except asyncio.TimeoutError:
        return {"error": "Analysis timed out."}
    except json.JSONDecodeError as e:
        return {"error": f"AI returned invalid JSON: {e}"}
    except Exception as e:
        return {"error": f"Analysis failed: {e}"}


_TREND_COLOR = {
    "improving":        "text-green-400",
    "stable":           "text-gray-300",
    "declining":        "text-red-400",
    "insufficient_data": "text-gray-500",
}
_TREND_ICON = {
    "improving":        "trending_up",
    "stable":           "trending_flat",
    "declining":        "trending_down",
    "insufficient_data": "help_outline",
}


def _render_signal_generator_panel(data: dict, analysis: dict):
    if "error" in analysis:
        with ui.card().classes("w-full bg-red-900/30 border border-red-700 p-4 mb-4"):
            ui.label(f"Signal generator analysis error: {analysis['error']}").classes(
                "text-red-300 text-sm"
            )
        return

    with ui.card().classes("w-full bg-gray-900 border border-purple-700 p-4 mb-4"):
        with ui.row().classes("items-center gap-3 mb-3 flex-wrap"):
            ui.icon("auto_awesome", color="purple-400")
            ui.label("Signal Generator Development").classes("text-purple-300 font-bold text-sm")
            ui.badge(f"{data['total_trades']} engine trades  |  {data['days']}d window",
                     color="purple").classes("text-xs")

        overall = analysis.get("overall_assessment", "")
        if overall:
            ui.label(overall).classes("text-gray-300 text-xs leading-relaxed mb-3")

        engines = analysis.get("engines", [])
        if engines:
            cols = min(len(engines), 3)
            with ui.element("div").style(
                f"display:grid; grid-template-columns:repeat({cols},1fr); gap:12px; width:100%;"
            ):
                for eng in engines:
                    trend     = eng.get("trend", "insufficient_data")
                    trend_cls = _TREND_COLOR.get(trend, "text-gray-400")
                    trend_ico = _TREND_ICON.get(trend, "help_outline")
                    is_pro    = eng.get("acting_like_pro_trader", False)
                    pro_cls   = "text-green-400" if is_pro else "text-red-400"
                    pro_lbl   = "Pro-level" if is_pro else "Not yet pro"

                    with ui.card().classes("bg-gray-800 border border-purple-900 rounded-lg p-3"):
                        with ui.row().classes("items-center gap-2 mb-2"):
                            ui.icon(trend_ico, color=trend_cls.replace("text-", ""), size="xs")
                            ui.label(eng.get("name", "")).classes(
                                "text-white font-semibold text-xs"
                            )
                            ui.space()
                            ui.label(pro_lbl).classes(f"text-xs font-bold {pro_cls}")

                        if eng.get("verdict"):
                            ui.label(eng["verdict"]).classes(
                                "text-xs text-gray-300 leading-relaxed mb-2"
                            )

                        ui.separator().classes("my-1")
                        for lbl, key, icon_name, icon_color in [
                            ("ML contribution",     "ml_contribution",       "memory",     "blue-300"),
                            ("Self-learning",        "self_learning_progress","school",     "teal-300"),
                            ("Key strength",         "key_strength",          "thumb_up",   "green-400"),
                            ("Key weakness",         "key_weakness",          "thumb_down", "orange-400"),
                            ("Recommendation",       "recommendation",        "lightbulb",  "yellow-300"),
                        ]:
                            val = eng.get(key, "")
                            if val:
                                with ui.row().classes("items-start gap-1 mt-1"):
                                    ui.icon(icon_name, color=icon_color, size="xs").classes(
                                        "mt-0.5 shrink-0"
                                    )
                                    with ui.column().classes("gap-0"):
                                        ui.label(lbl).classes("text-xs text-gray-500 font-semibold")
                                        ui.label(val).classes("text-xs text-gray-300 leading-relaxed")

        collective = analysis.get("collective_verdict", "")
        what_needed = analysis.get("what_would_make_them_professional", "")
        if collective or what_needed:
            ui.separator().classes("my-3")
            if collective:
                with ui.row().classes("items-start gap-2"):
                    ui.icon("groups", color="purple-300", size="xs").classes("mt-0.5")
                    ui.label("Collective verdict: ").classes(
                        "text-xs text-purple-300 font-semibold shrink-0"
                    )
                ui.label(collective).classes("text-xs text-gray-300 leading-relaxed mt-1")
            if what_needed:
                with ui.card().classes("bg-purple-900/20 border border-purple-800 rounded-lg p-3 mt-2"):
                    with ui.row().classes("items-center gap-2 mb-1"):
                        ui.icon("emoji_events", color="yellow-300", size="xs")
                        ui.label("What would make them professional?").classes(
                            "text-yellow-300 font-semibold text-xs"
                        )
                    ui.label(what_needed).classes("text-xs text-gray-200 leading-relaxed")


# ── UI ─────────────────────────────────────────────────────────────────────────

def render(get_engine: Callable):  # noqa: C901
    _state: dict = {"running": False}

    # ── Controls ───────────────────────────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 p-4 mb-4"):
        with ui.row().classes("items-center gap-4 flex-wrap"):
            ui.label("smart_toy").classes("material-icons text-yellow-400 text-xl")
            ui.label("AI Trade Analysis").classes("text-lg font-bold text-yellow-300")
            ui.badge("XAUUSD · Per-channel signal quality", color="amber").classes("text-xs")

            ui.space()

            days_sel = ui.select(
                {1: "Last 1 day", 3: "Last 3 days", 7: "Last 7 days",
                 14: "Last 14 days", 30: "Last 30 days"},
                value=7,
                label="Time window",
            ).classes("w-36")

            run_btn = ui.button("Run Analysis", icon="analytics").classes(
                "bg-yellow-600 hover:bg-yellow-500 text-white text-sm px-4 py-2"
            )

        status_lbl = ui.label("").classes("text-xs text-gray-400 mt-1")

    # ── Output area ────────────────────────────────────────────────────────────
    output = ui.row().classes("w-full gap-4 flex-wrap items-start")

    def _placeholder():
        output.clear()
        with output:
            with ui.card().classes("w-full bg-gray-900 p-8 text-center"):
                ui.label("smart_toy").classes("material-icons text-5xl text-gray-600 mb-3")
                ui.label(
                    "Click Run Analysis to evaluate Telegram signal quality against actual trade outcomes."
                ).classes("text-gray-400 text-sm")
                ui.label(
                    "Analysis covers: phantom TP detection, SL management, partial close modelling, "
                    "R:R reality check, session bias, consecutive loss patterns, lot sizing."
                ).classes("text-gray-600 text-xs mt-2")

    # ── Persistence helpers ────────────────────────────────────────────────────
    def _save(
        channels: list,
        analyses: dict,
        days: int,
        sdpm_data: Optional[dict] = None,
        sdpm_analysis: Optional[dict] = None,
        sg_data: Optional[dict] = None,
        sg_analysis: Optional[dict] = None,
    ) -> str:
        run_at = datetime.now().isoformat()
        try:
            payload = json.dumps({
                "channels":      channels,
                "analyses":      analyses,
                "days":          days,
                "run_at":        run_at,
                "sdpm_data":     sdpm_data,
                "sdpm_analysis": sdpm_analysis,
                "sg_data":       sg_data,
                "sg_analysis":   sg_analysis,
            })
            ai_ctl.set_app_config("ai_trade_analysis_last", payload)
        except Exception:
            pass
        return run_at

    def _load_stored() -> None:
        stored = ai_ctl.get_app_config("ai_trade_analysis_last")
        if not stored:
            _placeholder()
            return
        try:
            wrapper       = json.loads(stored)
            channels      = wrapper.get("channels", [])
            analyses      = wrapper.get("analyses", {})
            days          = wrapper.get("days", 7)
            run_at        = wrapper.get("run_at", "")
            sdpm_data     = wrapper.get("sdpm_data")
            sdpm_analysis = wrapper.get("sdpm_analysis")
            sg_data       = wrapper.get("sg_data")
            sg_analysis   = wrapper.get("sg_analysis")

            if not channels and not sdpm_data and not sg_data:
                _placeholder()
                return

            output.clear()
            with output:
                if sg_data and sg_analysis:
                    with ui.element("div").classes("w-full"):
                        _render_signal_generator_panel(sg_data, sg_analysis)
                if sdpm_data and sdpm_analysis:
                    with ui.element("div").classes("w-full"):
                        _render_strategy_dpm_panel(sdpm_data, sdpm_analysis)
                for ch in channels:
                    with ui.column().classes("flex-1 min-w-80 gap-3"):
                        _render_channel_header(ch)
                        with ui.column().classes("w-full gap-3"):
                            stored_analysis = analyses.get(ch["group_id"], {})
                            if stored_analysis:
                                _render_channel_analysis(ch, stored_analysis)
                            else:
                                with ui.card().classes("w-full bg-gray-800 p-4"):
                                    ui.label("No stored analysis for this channel.").classes(
                                        "text-xs text-gray-500"
                                    )

            days_sel.value = days

            if run_at:
                try:
                    dt = datetime.fromisoformat(run_at)
                    status_lbl.text = (
                        f"Last run: {dt.strftime('%d %b %Y %H:%M')}  |  "
                        f"{len(channels)} channel(s)  |  {days}d window"
                    )
                except Exception:
                    status_lbl.text = f"Loaded from last session | {len(channels)} channel(s)"

        except Exception:
            _placeholder()

    _load_stored()

    async def _run():
        if _state["running"]:
            return
        _state["running"] = True
        run_btn.disable()
        status_lbl.text = "Gathering data..."

        cfg = cfg_module.load_config()
        env = cfg.get("account_env", "demo")
        from backend.src.controllers.settings_controller import DATA_DIR
        db_path   = str(DATA_DIR / f"forex_trader_{env}.db")
        days      = int(days_sel.value)

        # Gather raw data in executor (synchronous DB queries)
        try:
            channels, sdpm_data, sg_data = await asyncio.gather(
                asyncio.get_running_loop().run_in_executor(
                    None, _gather_channel_data, db_path, days
                ),
                asyncio.get_running_loop().run_in_executor(
                    None, _gather_strategy_dpm_data, db_path, days
                ),
                asyncio.get_running_loop().run_in_executor(
                    None, _gather_signal_generator_data, db_path, days
                ),
            )
        except Exception as e:
            status_lbl.text = f"Data error: {e}"
            _state["running"] = False
            run_btn.enable()
            return

        # Build initial layout with loading placeholders
        output.clear()
        panel_containers: dict[str, ui.column] = {}
        with output:
            # Signal generator panel — full width, at top
            sg_container = ui.element("div").classes("w-full")
            with sg_container:
                with ui.card().classes(
                    "w-full bg-gray-800 border border-purple-800 p-6 text-center"
                ):
                    with ui.row().classes("items-center gap-3 justify-center"):
                        ui.spinner("dots", size="lg", color="purple")
                        ui.label("Analysing signal generator development with AI...").classes(
                            "text-gray-400 text-sm"
                        )
            # Strategy/DPM panel — full width, below sg panel
            sdpm_container = ui.element("div").classes("w-full")
            with sdpm_container:
                with ui.card().classes(
                    "w-full bg-gray-800 border border-teal-800 p-6 text-center"
                ):
                    with ui.row().classes("items-center gap-3 justify-center"):
                        ui.spinner("dots", size="lg", color="teal")
                        ui.label("Analysing strategy vs DPM with AI...").classes(
                            "text-gray-400 text-sm"
                        )
            # Per-channel panels
            for ch in channels:
                with ui.column().classes("flex-1 min-w-80 gap-3"):
                    _render_channel_header(ch)
                    panel = ui.column().classes("w-full gap-3")
                    with panel:
                        with ui.card().classes("w-full bg-gray-800 p-6 text-center"):
                            with ui.row().classes("items-center gap-3 justify-center"):
                                ui.spinner("dots", size="lg", color="yellow")
                                ui.label("Analysing with AI...").classes("text-gray-400 text-sm")
                    panel_containers[ch["group_id"]] = panel

        if not channels and sdpm_data.get("total_closed", 0) == 0 and sg_data.get("total_trades", 0) == 0:
            output.clear()
            with output:
                with ui.card().classes("w-full bg-gray-900 p-6 text-center"):
                    ui.label("No trade history found in the selected time window.").classes(
                        "text-gray-400 text-sm"
                    )
                    ui.label(
                        "Try extending the period or confirm signals are being received from Telegram."
                    ).classes("text-gray-600 text-xs mt-1")
            status_lbl.text = "No data found."
            _state["running"] = False
            run_btn.enable()
            return

        # Run signal generator analysis
        status_lbl.text = "Analysing signal generator development..."
        sg_analysis = await run_signal_generator_analysis(sg_data, cfg)
        sg_container.clear()
        with sg_container:
            _render_signal_generator_panel(sg_data, sg_analysis)

        # Run strategy/DPM analysis
        status_lbl.text = "Analysing strategy vs DPM performance..."
        sdpm_analysis = await run_strategy_dpm_analysis(sdpm_data, cfg)
        sdpm_container.clear()
        with sdpm_container:
            _render_strategy_dpm_panel(sdpm_data, sdpm_analysis)

        # Run analysis for each channel sequentially
        total = len(channels)
        analyses: dict[str, dict] = {}
        for idx, ch in enumerate(channels):
            cname = ch["channel_name"]
            status_lbl.text = f"Analysing {cname} ({idx + 1}/{total})..."

            analysis = await run_channel_analysis(ch, days, cfg)
            analyses[ch["group_id"]] = analysis

            panel = panel_containers[ch["group_id"]]
            panel.clear()
            with panel:
                _render_channel_analysis(ch, analysis)

        run_at = _save(channels, analyses, days, sdpm_data, sdpm_analysis, sg_data, sg_analysis)
        try:
            dt = datetime.fromisoformat(run_at)
            status_lbl.text = (
                f"Last run: {dt.strftime('%d %b %Y %H:%M')}  |  "
                f"{total} channel(s)  |  {days}d window"
            )
        except Exception:
            status_lbl.text = f"Analysis complete ({total} channel(s) — {days}d window)."

        _state["running"] = False
        run_btn.enable()

    run_btn.on("click", _run)


# ── Channel header card ────────────────────────────────────────────────────────

def _render_channel_header(ch: dict):
    stats = ch["stats"]
    with ui.card().classes("w-full bg-gray-900 p-4"):
        with ui.row().classes("items-center gap-2 mb-2"):
            ui.icon("telegram", color="blue-400")
            ui.label(ch["channel_name"]).classes("text-white font-bold text-sm")

        with ui.row().classes("gap-3 flex-wrap"):
            for label, val, cls in [
                (f"{stats['total_signals']} signals", "", "text-gray-300"),
                (f"{stats['closed_trades']} closed", "", "text-gray-300"),
                (f"{float(stats['win_rate_pct'] or 0):.0f}% win rate",
                 "", "text-green-400" if (stats["win_rate_pct"] or 0) >= 50 else "text-red-400"),
                (f"${float(stats['total_pnl'] or 0):+.2f}",
                 "", "text-green-400" if (stats["total_pnl"] or 0) >= 0 else "text-red-400"),
                (f"{stats['phantom_tp_count']} phantom TPs",
                 "", "text-orange-400" if stats["phantom_tp_count"] else "text-gray-500"),
            ]:
                ui.label(label).classes(f"text-xs font-semibold {cls}")


# ── Main analysis render ───────────────────────────────────────────────────────

def _render_channel_analysis(ch: dict, analysis: dict):  # noqa: C901
    if "error" in analysis:
        with ui.card().classes("w-full bg-red-900/30 border border-red-700 p-4"):
            ui.label(f"Analysis error: {analysis['error']}").classes("text-red-300 text-sm")
        return

    stats = ch["stats"]

    # ── Score card ─────────────────────────────────────────────────────────────
    score = int(analysis.get("reliability_score", 0))
    score_cls = "text-green-400" if score >= 75 else "text-yellow-400" if score >= 50 else "text-red-400"
    score_bg  = "bg-green-900/30 border-green-700" if score >= 75 else \
                "bg-yellow-900/30 border-yellow-700" if score >= 50 else \
                "bg-red-900/30 border-red-700"

    with ui.card().classes(f"w-full {score_bg} border p-4"):
        with ui.row().classes("items-center gap-4"):
            ui.label(f"{score}").classes(f"text-4xl font-black {score_cls}")
            with ui.column().classes("gap-0"):
                ui.label("/100 Reliability").classes("text-xs text-gray-400")
                ui.label(analysis.get("reliability_label", "")).classes("text-white text-sm font-semibold")
        ui.separator().classes("my-2")
        ui.label(analysis.get("executive_summary", "")).classes("text-gray-300 text-xs leading-relaxed")

    # ── Pre-flight stats ───────────────────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-900 p-4"):
        with ui.grid(columns=3).classes("w-full gap-2 text-center"):
            for lbl, val, cls in [
                ("Win Rate",    f"{float(stats['win_rate_pct'] or 0):.1f}%",   "text-white"),
                ("SL Hits",     str(stats["sl_hits"]),              "text-red-400"),
                ("Phantom TPs", str(stats["phantom_tp_count"]),     "text-orange-400" if stats["phantom_tp_count"] else "text-gray-500"),
                ("Max Consec.", f"{stats['max_consecutive_losses']} losses", "text-red-400" if (stats["max_consecutive_losses"] or 0) >= 3 else "text-gray-300"),
                ("Actual P&L",  f"${float(stats['actual_pnl_sum'] or 0):+.2f}", "text-green-400" if (stats["actual_pnl_sum"] or 0) >= 0 else "text-red-400"),
                ("Sim. P&L",    f"${float(stats['simulated_50pct_pnl_sum'] or 0):+.2f}", "text-blue-400"),
            ]:
                with ui.column().classes("items-center gap-0.5 bg-gray-800 rounded p-2"):
                    ui.label(val).classes(f"text-sm font-bold {cls}")
                    ui.label(lbl).classes("text-xs text-gray-500")

    # ── Phantom TP section ─────────────────────────────────────────────────────
    phantoms = analysis.get("phantom_tps", [])
    phantom_pattern = analysis.get("phantom_tp_pattern", "")
    if phantoms or phantom_pattern:
        with ui.card().classes("w-full bg-red-900/20 border border-red-800 p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("warning", color="red-400")
                ui.label("Phantom TP Detection").classes("text-red-300 font-semibold text-sm")

            if phantom_pattern:
                ui.label(phantom_pattern).classes("text-gray-300 text-xs mb-2 leading-relaxed")

            for ph in phantoms:
                with ui.row().classes("items-start gap-2 py-1 border-t border-red-900"):
                    with ui.column().classes("gap-0"):
                        ui.label(
                            f"{ph.get('date', '?')} {ph.get('direction', '')}"
                        ).classes("text-xs text-gray-400 font-mono")
                        ui.label(
                            f"Claimed: {ph.get('claimed', '?')}  |  Actual: {ph.get('actual', '?')}  |  "
                            f"P&L: ${float(ph.get('pnl', 0)):+.2f}"
                        ).classes("text-xs text-red-300")
                        if ph.get("detail"):
                            ui.label(ph["detail"]).classes("text-xs text-gray-500 mt-0.5")

    # ── SL management ─────────────────────────────────────────────────────────
    sl_mgmt = analysis.get("sl_management", {})
    if sl_mgmt:
        has_gap = not sl_mgmt.get("channel_instructs_be", True)
        bg = "bg-orange-900/20 border-orange-800" if has_gap else "bg-gray-900"
        with ui.card().classes(f"w-full {bg} border p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("security", color="orange-300")
                ui.label("SL Management").classes("text-orange-300 font-semibold text-sm")
            if sl_mgmt.get("current_weakness"):
                ui.label(f"Issue: {sl_mgmt['current_weakness']}").classes(
                    "text-xs text-gray-300 mb-1"
                )
            if sl_mgmt.get("recommended_rule"):
                with ui.row().classes("items-start gap-2 bg-gray-800 rounded p-2 mt-1"):
                    ui.icon("check_circle", color="green-400", size="xs")
                    ui.label(sl_mgmt["recommended_rule"]).classes(
                        "text-xs text-green-300 font-semibold flex-1"
                    )
            if sl_mgmt.get("implementation_note"):
                ui.label(sl_mgmt["implementation_note"]).classes(
                    "text-xs text-gray-500 mt-1 italic"
                )

    # ── Partial close model ────────────────────────────────────────────────────
    pcm = analysis.get("partial_close_model", {})
    if pcm:
        improvement = float(pcm.get("improvement", 0))
        imp_cls = "text-green-400" if improvement > 0 else "text-red-400"
        with ui.card().classes("w-full bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("call_split", color="blue-300")
                ui.label("Partial Close Model (50% at TP1)").classes("text-blue-300 font-semibold text-sm")
            with ui.row().classes("gap-4"):
                for lbl, val, cls in [
                    ("Actual P&L",     f"${float(pcm.get('actual_period_pnl', 0)):+.2f}", "text-red-400" if float(pcm.get('actual_period_pnl', 0)) < 0 else "text-green-400"),
                    ("Simulated P&L",  f"${float(pcm.get('simulated_50pct_tp1_pnl', 0)):+.2f}", "text-blue-400"),
                    ("Improvement",    f"${improvement:+.2f}", imp_cls),
                ]:
                    with ui.column().classes("items-center bg-gray-800 rounded px-3 py-2 gap-0"):
                        ui.label(val).classes(f"text-sm font-bold {cls}")
                        ui.label(lbl).classes("text-xs text-gray-500")
            if pcm.get("verdict"):
                ui.label(pcm["verdict"]).classes("text-xs text-gray-400 mt-2 italic leading-relaxed")

    # ── R:R analysis ──────────────────────────────────────────────────────────
    rr = analysis.get("rr_analysis", {})
    if rr and (rr.get("signals_below_1_to_1", 0) > 0 or rr.get("comment")):
        with ui.card().classes("w-full bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("balance", color="purple-300")
                ui.label("R:R Reality Check").classes("text-purple-300 font-semibold text-sm")
            if rr.get("comment"):
                ui.label(rr["comment"]).classes("text-xs text-gray-300 leading-relaxed")
            for flag in (rr.get("flags") or []):
                with ui.row().classes("items-center gap-1 mt-1"):
                    ui.icon("flag", color="orange-400", size="xs")
                    ui.label(flag).classes("text-xs text-orange-300")

    # ── Session + Entry drift (side-by-side) ───────────────────────────────────
    with ui.row().classes("w-full gap-3"):
        with ui.card().classes("flex-1 bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("schedule", color="cyan-300")
                ui.label("Session Bias").classes("text-cyan-300 font-semibold text-sm")
            ui.label(analysis.get("session_analysis", "—")).classes(
                "text-xs text-gray-300 leading-relaxed"
            )
            if stats["session_pnl"]:
                ui.separator().classes("my-1")
                for sess, pnl in sorted(stats["session_pnl"].items(), key=lambda x: x[1]):
                    col = "text-green-400" if pnl >= 0 else "text-red-400"
                    ui.label(f"{sess}: ${pnl:+.2f}").classes(f"text-xs font-mono {col}")

        drift = analysis.get("entry_drift", {})
        if drift:
            with ui.card().classes("flex-1 bg-gray-900 p-4"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("swap_horiz", color="teal-300")
                    ui.label("Entry Drift").classes("text-teal-300 font-semibold text-sm")
                with ui.row().classes("gap-3"):
                    for lbl, val in [("Avg pips", f"{drift.get('avg_pips', 0):.1f}"),
                                     ("Worst", f"{drift.get('worst_case_pips', 0):.1f}")]:
                        with ui.column().classes("items-center bg-gray-800 rounded px-3 py-1 gap-0"):
                            ui.label(val).classes("text-sm font-bold text-white")
                            ui.label(lbl).classes("text-xs text-gray-500")
                if drift.get("comment"):
                    ui.label(drift["comment"]).classes("text-xs text-gray-400 mt-2 leading-relaxed")

    # ── Consecutive losses ─────────────────────────────────────────────────────
    consec = analysis.get("consecutive_losses", "")
    if consec:
        with ui.card().classes("w-full bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("trending_down", color="red-300")
                ui.label("Consecutive Loss Patterns").classes("text-red-300 font-semibold text-sm")
            ui.label(consec).classes("text-xs text-gray-300 leading-relaxed")

    # ── Lot sizing ─────────────────────────────────────────────────────────────
    ls = analysis.get("lot_sizing", {})
    if ls:
        with ui.card().classes("w-full bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("percent", color="lime-300")
                ui.label("Lot Sizing Recommendation").classes("text-lime-300 font-semibold text-sm")
            with ui.row().classes("gap-3"):
                for lbl, val, cls in [
                    ("Actual Win Rate",   f"{ls.get('actual_win_rate_pct', 0):.1f}%",  "text-white"),
                    ("Kelly Fraction",    f"{ls.get('kelly_fraction_pct', 0):.1f}%",   "text-yellow-300"),
                    ("Recommended Risk",  f"{ls.get('recommended_risk_pct', 0):.2f}%", "text-green-400"),
                ]:
                    with ui.column().classes("items-center bg-gray-800 rounded px-3 py-2 gap-0"):
                        ui.label(val).classes(f"text-sm font-bold {cls}")
                        ui.label(lbl).classes("text-xs text-gray-500")
            if ls.get("reasoning"):
                ui.label(ls["reasoning"]).classes("text-xs text-gray-400 mt-2 leading-relaxed")

    # ── Overall recommendation ─────────────────────────────────────────────────
    rec = analysis.get("overall_recommendation", "")
    if rec:
        with ui.card().classes("w-full bg-blue-900/20 border border-blue-800 p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("lightbulb", color="blue-300")
                ui.label("Overall Recommendation").classes("text-blue-300 font-semibold text-sm")
            ui.label(rec).classes("text-gray-200 text-xs leading-relaxed whitespace-pre-line")


# ── Strategy vs DPM comparison panel ──────────────────────────────────────────

_APPROACH_LABELS = {
    "dpm":              "DPM (Adaptive)",
    "scale_out":        "Scale Out",
    "be_runner":        "BE Runner",
    "trail_stop":       "Trail Stop",
    "protected_scale":  "Protected Scale",
    "insufficient_data": "Insufficient Data",
}

_REC_COLOR = {
    "keep_dpm":           "text-green-400",
    "disable_dpm":        "text-red-400",
    "use_dpm_selectively": "text-yellow-400",
}


def _render_strategy_dpm_panel(data: dict, analysis: dict):  # noqa: C901
    if "error" in analysis:
        with ui.card().classes("w-full bg-red-900/30 border border-red-700 p-4 mb-4"):
            ui.label(f"Strategy/DPM analysis error: {analysis['error']}").classes(
                "text-red-300 text-sm"
            )
        return

    best = analysis.get("best_approach", "insufficient_data")
    best_label = _APPROACH_LABELS.get(best, best)
    dpm_rec    = analysis.get("dpm_assessment", {}).get("recommendation", "")
    rec_cls    = _REC_COLOR.get(dpm_rec, "text-white")

    with ui.card().classes(
        "w-full bg-gray-900 border border-teal-700 p-4 mb-4"
    ):
        # Header
        with ui.row().classes("items-center gap-3 mb-3 flex-wrap"):
            ui.icon("tune", color="teal-400")
            ui.label("Strategy vs DPM Analysis").classes(
                "text-teal-300 font-bold text-sm"
            )
            ui.badge(
                f"Best approach: {best_label}",
                color="teal",
            ).classes("text-xs")
            ui.space()
            ui.label(
                f"{data['total_closed']} closed trades  |  {data['days']}d window"
            ).classes("text-xs text-gray-500")

        # Head-to-head stats grid
        dpm_s   = data["dpm_stats"]
        fixed_s = data["fixed_stats"]

        with ui.element("div").style(
            "display:grid; grid-template-columns:repeat(2,1fr); gap:12px; width:100%;"
        ):
            for label, s, border_cls in [
                ("DPM-Managed Trades",          dpm_s,   "border-teal-700"),
                ("Fixed-Strategy Trades",        fixed_s, "border-gray-600"),
            ]:
                bg_cls = "bg-teal-900/20" if "DPM" in label else "bg-gray-800"
                with ui.card().classes(f"{bg_cls} border {border_cls} rounded-lg p-3"):
                    ui.label(label).classes("text-xs text-gray-400 font-semibold mb-2 uppercase")
                    if s["count"] == 0:
                        ui.label("No trades in period").classes("text-xs text-gray-600 italic")
                    else:
                        with ui.element("div").style(
                            "display:grid; grid-template-columns:repeat(3,1fr); gap:6px;"
                        ):
                            for lbl, val, cls in [
                                ("Trades",    str(s["count"]),                   "text-white"),
                                ("Win Rate",  f"{s['win_rate']}%",               "text-green-400" if s["win_rate"] >= 50 else "text-red-400"),
                                ("Total P&L", f"${s['total_pnl']:+.2f}",         "text-green-400" if s["total_pnl"] >= 0 else "text-red-400"),
                                ("Avg P&L",   f"${s['avg_pnl']:+.2f}",           "text-green-400" if s["avg_pnl"] >= 0 else "text-red-400"),
                                ("Prof. Factor", f"{s['profit_factor']}",        "text-yellow-300"),
                                ("Avg Hold",  f"{s['avg_hold_min']:.0f}m",       "text-gray-300"),
                            ]:
                                with ui.column().classes("items-center gap-0 bg-gray-900 rounded p-1"):
                                    ui.label(val).classes(f"text-xs font-bold {cls}")
                                    ui.label(lbl).classes("text-xs text-gray-600")

        # Per fixed-strategy breakdown
        if data["strategy_breakdown"]:
            ui.separator().classes("my-3")
            ui.label("Fixed Strategy Breakdown").classes(
                "text-xs text-gray-400 uppercase font-semibold mb-2"
            )
            cols = min(len(data["strategy_breakdown"]), 4)
            with ui.element("div").style(
                f"display:grid; grid-template-columns:repeat({cols},1fr); gap:8px; width:100%;"
            ):
                for s in data["strategy_breakdown"]:
                    sl = _APPROACH_LABELS.get(s["strategy"], s["strategy"])
                    pnl_cls = "text-green-400" if s["total_pnl"] >= 0 else "text-red-400"
                    with ui.card().classes("bg-gray-800 rounded-lg p-2"):
                        ui.label(sl).classes("text-xs text-gray-300 font-semibold mb-1")
                        ui.label(f"{s['win_rate']}% WR  |  {s['count']} trades").classes(
                            "text-xs text-gray-400"
                        )
                        ui.label(f"${s['total_pnl']:+.2f}  PF {s['profit_factor']}").classes(
                            f"text-xs font-mono font-bold {pnl_cls}"
                        )

        # DPM detail
        d = data["dpm_detail"]
        if d["count"] > 0:
            ui.separator().classes("my-3")
            with ui.row().classes("gap-2 flex-wrap mb-2"):
                ui.label("DPM Detail").classes(
                    "text-xs text-gray-400 uppercase font-semibold"
                )
                ui.badge(f"Avg R: {d['avg_r_multiple']}", color="teal").classes("text-xs")
                if d["avg_trail_capture"] is not None:
                    ui.badge(
                        f"Trail capture: {d['avg_trail_capture']:.0%}", color="blue"
                    ).classes("text-xs")
                if d["calibrated_trades"] > 0:
                    ui.badge(
                        f"{d['calibrated_trades']} calibrated", color="green"
                    ).classes("text-xs")

            with ui.row().classes("gap-3 flex-wrap"):
                # Regime breakdown
                if d["regime_breakdown"]:
                    with ui.card().classes("bg-gray-800 rounded-lg p-3 flex-1 min-w-40"):
                        ui.label("Regime").classes("text-xs text-gray-500 uppercase mb-1")
                        for reg, rs in sorted(
                            d["regime_breakdown"].items(),
                            key=lambda x: x[1]["pnl"],
                            reverse=True,
                        ):
                            wr = round(rs["wins"] / rs["count"] * 100) if rs["count"] else 0
                            pnl_cls = "text-green-400" if rs["pnl"] >= 0 else "text-red-400"
                            with ui.row().classes("items-center justify-between gap-1"):
                                ui.label(reg).classes("text-xs text-gray-300 capitalize")
                                ui.label(f"${rs['pnl']:+.2f}  {wr}%").classes(
                                    f"text-xs font-mono {pnl_cls}"
                                )
                # Session breakdown
                if d["session_breakdown"]:
                    with ui.card().classes("bg-gray-800 rounded-lg p-3 flex-1 min-w-40"):
                        ui.label("Session").classes("text-xs text-gray-500 uppercase mb-1")
                        for sess, rs in sorted(
                            d["session_breakdown"].items(),
                            key=lambda x: x[1]["pnl"],
                            reverse=True,
                        ):
                            wr = round(rs["wins"] / rs["count"] * 100) if rs["count"] else 0
                            pnl_cls = "text-green-400" if rs["pnl"] >= 0 else "text-red-400"
                            with ui.row().classes("items-center justify-between gap-1"):
                                ui.label(sess).classes("text-xs text-gray-300 capitalize")
                                ui.label(f"${rs['pnl']:+.2f}  {wr}%").classes(
                                    f"text-xs font-mono {pnl_cls}"
                                )
                # Exit type breakdown
                if d["exit_breakdown"]:
                    with ui.card().classes("bg-gray-800 rounded-lg p-3 flex-1 min-w-40"):
                        ui.label("Exit Type").classes("text-xs text-gray-500 uppercase mb-1")
                        for et, rs in sorted(
                            d["exit_breakdown"].items(),
                            key=lambda x: x[1]["count"],
                            reverse=True,
                        ):
                            pnl_cls = "text-green-400" if rs["pnl"] >= 0 else "text-red-400"
                            with ui.row().classes("items-center justify-between gap-1"):
                                ui.label(et).classes("text-xs text-gray-300")
                                ui.label(f"{rs['count']}×  ${rs['pnl']:+.2f}").classes(
                                    f"text-xs font-mono {pnl_cls}"
                                )

        # Claude analysis
        ui.separator().classes("my-3")
        verdict = analysis.get("overall_verdict", "")
        if verdict:
            ui.label(verdict).classes(
                "text-gray-200 text-xs leading-relaxed mb-2"
            )

        dpm_a = analysis.get("dpm_assessment", {})
        if dpm_a:
            with ui.card().classes("bg-teal-900/20 border border-teal-800 rounded-lg p-3"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("psychology", color="teal-300", size="xs")
                    ui.label("DPM Assessment").classes("text-teal-300 font-semibold text-xs")
                    if dpm_rec:
                        rec_label = {
                            "keep_dpm":           "Keep DPM",
                            "disable_dpm":        "Disable DPM",
                            "use_dpm_selectively": "Use Selectively",
                        }.get(dpm_rec, dpm_rec)
                        ui.badge(rec_label, color="teal").classes(f"text-xs {rec_cls}")
                if dpm_a.get("verdict"):
                    ui.label(dpm_a["verdict"]).classes("text-xs text-gray-300 mb-1 leading-relaxed")
                with ui.row().classes("gap-3 flex-wrap"):
                    if dpm_a.get("strength"):
                        with ui.column().classes("flex-1 min-w-32 gap-0"):
                            ui.label("Strength").classes("text-xs text-green-400 font-semibold")
                            ui.label(dpm_a["strength"]).classes("text-xs text-gray-400")
                    if dpm_a.get("weakness"):
                        with ui.column().classes("flex-1 min-w-32 gap-0"):
                            ui.label("Weakness").classes("text-xs text-red-400 font-semibold")
                            ui.label(dpm_a["weakness"]).classes("text-xs text-gray-400")
                if dpm_a.get("best_regime") or dpm_a.get("best_session"):
                    with ui.row().classes("gap-2 mt-1 flex-wrap"):
                        if dpm_a.get("best_regime"):
                            ui.badge(
                                f"Best regime: {dpm_a['best_regime']}", color="gray"
                            ).classes("text-xs")
                        if dpm_a.get("best_session"):
                            ui.badge(
                                f"Best session: {dpm_a['best_session']}", color="gray"
                            ).classes("text-xs")

        strat_notes = analysis.get("strategy_notes", [])
        if strat_notes:
            ui.separator().classes("my-2")
            ui.label("Fixed Strategy Notes").classes("text-xs text-gray-500 uppercase mb-1")
            for sn in strat_notes:
                sl = _APPROACH_LABELS.get(sn.get("strategy", ""), sn.get("strategy", ""))
                with ui.row().classes("items-start gap-2"):
                    ui.label(sl).classes("text-xs text-gray-300 font-semibold min-w-28")
                    ui.label(sn.get("verdict", "")).classes("text-xs text-gray-500 flex-1")

        if analysis.get("when_to_use_fixed"):
            ui.separator().classes("my-2")
            with ui.row().classes("items-start gap-2"):
                ui.icon("help_outline", color="gray-400", size="xs")
                ui.label("When to use fixed strategies: ").classes(
                    "text-xs text-gray-400 font-semibold"
                )
            ui.label(analysis["when_to_use_fixed"]).classes(
                "text-xs text-gray-400 leading-relaxed"
            )

        advice = analysis.get("actionable_advice", "")
        if advice:
            ui.separator().classes("my-2")
            with ui.card().classes("w-full bg-blue-900/20 border border-blue-800 rounded-lg p-3"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("lightbulb", color="blue-300", size="xs")
                    ui.label("Actionable Advice").classes("text-blue-300 font-semibold text-xs")
                ui.label(advice).classes("text-gray-200 text-xs leading-relaxed whitespace-pre-line")
