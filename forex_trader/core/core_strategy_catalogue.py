"""The full set of things a trade can actually be managed by, in one place
(2026-08-07), so an AI recommendation can choose from all of them.

Three kinds live side by side in this app and, until now, only the first
two were ever shown to the model asking for a "recommended strategy" --
and only six of the built-ins at that, hardcoded in claude_ai.py's prompt:

  builtin   -- the pre-coded Python strategies in models.STRATEGY_NAMES
  custom    -- user-defined variants in the custom_strategies table
  template  -- EA-native trade-management definitions in the
               ea_trade_templates table (core_ea_templates.py), keyed
               "template:<name>" exactly as a channel override keys them

Templates are the reason this module exists rather than a longer literal
list in the prompt: they are created and edited by the user at runtime, so
the model can only recommend one if it is told, on every call, which
templates exist right now and what each one actually does. _template_summary
therefore renders a template's live field values into prose rather than
naming it and hoping the name is descriptive.

Consumers: claude_ai.request_market_analysis (prompt + validation) and
ui/pages/ai_summary.py (rendering the recommendation it gets back).
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

KIND_BUILTIN  = "builtin"
KIND_CUSTOM   = "custom"
KIND_TEMPLATE = "template"

# Longest single-entry summary put in front of the model. Template summaries
# are generated from up to ~30 fields, so without a cap a handful of heavily
# configured templates could crowd out the market data in the same prompt.
_MAX_SUMMARY = 400


def get_hidden_strategies() -> set[str]:
    """Built-in/custom strategy ids the user has hidden from the Trading >
    Strategy pickers. Hidden means "I never want to use this", so hidden
    entries are left out of the catalogue too -- recommending one would be
    advice the user cannot act on."""
    from forex_trader.core import database as db_module

    raw = db_module.get_app_config("hidden_strategies") or "[]"
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def _first_line(text: str, limit: int = _MAX_SUMMARY) -> str:
    """Reduce a markdown STRATEGY_DESCRIPTIONS blob to one plain-text line."""
    line = (text or "").split("\n")[0].strip()
    line = line.replace("**", "").replace("`", "")
    if line.startswith("#"):
        line = line.lstrip("#").strip()
    return line[:limit].strip()


def _fmt(v: float) -> str:
    """Trim the trailing .0 that every DEFAULTS float carries."""
    return f"{v:g}"


def _ladder(tpl: dict, prefix: str, max_levels: int) -> str:
    """Render a template's TP ladder as "30/50/80 pips at 40/30/30%"."""
    pips: list[str] = []
    pcts: list[str] = []
    for n in range(1, max_levels + 1):
        p = float(tpl.get(f"{prefix}{n}_pips") or 0)
        c = float(tpl.get(f"{prefix}{n}_pct") or 0)
        if p <= 0 and c <= 0:
            continue
        pips.append(_fmt(p))
        pcts.append(_fmt(c))
    if not pips:
        return ""
    return f"{'/'.join(pips)} pips at {'/'.join(pcts)}%"


def _template_summary(tpl: dict) -> str:
    """Describe what a template does, from its own stored field values.

    The user creates and tunes these at runtime, so the name alone tells the
    model nothing -- this is what makes a brand-new template recommendable
    on the very next analysis run.
    """
    from forex_trader.core.core_ea_templates import MAX_TP_LEVELS

    parts: list[str] = []

    mode = tpl.get("mode") or "single"
    if mode == "grid":
        pend_mode = tpl.get("pending_mode") or "zone"
        where = ("legs spanning the signal's entry zone" if pend_mode == "zone"
                 else f"legs stepped {_fmt(float(tpl.get('grid_step_pts') or 0))} pts apart")
        parts.append(
            f"EA-managed grid: {int(tpl.get('anchors') or 0)} anchor leg(s) at market "
            f"({_fmt(float(tpl.get('lot_anchor') or 0))} lot) plus "
            f"{int(tpl.get('pendings') or 0)} pending leg(s) "
            f"({_fmt(float(tpl.get('lot_pending') or 0))} lot), {where}"
        )
    else:
        parts.append(
            f"EA-managed single entry at market "
            f"({_fmt(float(tpl.get('lot_anchor') or 0))} lot)"
        )

    risk_pct = float(tpl.get("risk_pct") or 0)
    if risk_pct > 0:
        parts.append(f"sized at {_fmt(risk_pct)}% account risk")

    if tpl.get("use_dynamic_atr"):
        parts.append(
            f"SL from ATR({int(tpl.get('atr_period') or 14)}) "
            f"x {_fmt(float(tpl.get('atr_sl_mult') or 0))}"
        )
    else:
        parts.append(f"SL {_fmt(float(tpl.get('sl_pips') or 0))} pips")

    anchor_ladder = _ladder(tpl, "tp", MAX_TP_LEVELS)
    if tpl.get("tp_from_telegram"):
        parts.append("TP levels taken from the signal message")
    elif anchor_ladder:
        parts.append(f"TP ladder {anchor_ladder}")
    else:
        parts.append("no TP ladder configured")

    if mode == "grid":
        pen_ladder = _ladder(tpl, "tp_pen", MAX_TP_LEVELS)
        if tpl.get("tp_pen_from_telegram"):
            parts.append("pending legs take TPs from the signal message")
        elif pen_ladder:
            parts.append(f"pending-leg ladder {pen_ladder}")

    if not tpl.get("partials"):
        parts.append("single close at the final level (no partials)")
    elif not tpl.get("close_full_on_last"):
        parts.append("leaves a runner past the last configured TP")

    trail = tpl.get("trail_mode") or "off"
    if trail != "off":
        parts.append(
            f"{trail} trail {_fmt(float(tpl.get('trail_distance') or 0))} pips, "
            f"step {_fmt(float(tpl.get('trail_step') or 0))}, "
            f"armed at {_fmt(float(tpl.get('trail_activation') or 0))}"
        )

    be_mode = tpl.get("be_mode") or "entry"
    be_txt = f"breakeven at TP{int(tpl.get('be_trigger') or 1)}"
    if be_mode == "entry_buffer":
        be_txt += f" +{_fmt(float(tpl.get('be_buffer_pts') or 0))} pts"
    parts.append(be_txt)

    guards: list[str] = []
    if tpl.get("sig_guard"):
        pips = float(tpl.get("sig_guard_pips") or 0)
        guards.append("sig guard" + (f" {_fmt(pips)} pips" if pips > 0 else ""))
    if float(tpl.get("late_guard_pips") or 0) > 0:
        guards.append(f"late guard {_fmt(float(tpl['late_guard_pips']))} pips")
    if float(tpl.get("signal_rr_ratio") or 0) > 0:
        guards.append(f"min signal R:R {_fmt(float(tpl['signal_rr_ratio']))}")
    if float(tpl.get("max_spread_pips") or 0) > 0:
        guards.append(f"max spread {_fmt(float(tpl['max_spread_pips']))} pips")
    if float(tpl.get("equity_protect") or 0) > 0:
        guards.append(f"equity protect ${_fmt(float(tpl['equity_protect']))}")
    if guards:
        parts.append("guards: " + ", ".join(guards))

    if tpl.get("harvest_enabled"):
        parts.append(
            f"harvests profit at ${_fmt(float(tpl.get('harvest_threshold') or 0))}"
        )

    return "; ".join(parts)[:_MAX_SUMMARY]


def build_catalogue(include_hidden: bool = False) -> list[dict]:
    """Every recommendable option, as [{key, label, kind, summary}].

    Reads the database on each call deliberately: a template the user saved a
    minute ago has to be recommendable on the next analysis run, so nothing
    here is cached.
    """
    from forex_trader.core import core_ea_templates as ea_templates
    from forex_trader.core import database as db_module
    from forex_trader.core.models import STRATEGY_DESCRIPTIONS, STRATEGY_NAMES

    hidden = set() if include_hidden else get_hidden_strategies()
    entries: list[dict] = []

    for key, label in STRATEGY_NAMES.items():
        if key in hidden:
            continue
        entries.append({
            "key":     key,
            "label":   label,
            "kind":    KIND_BUILTIN,
            "summary": _first_line(STRATEGY_DESCRIPTIONS.get(key, "")) or label,
        })

    try:
        for cs in db_module.get_custom_strategies():
            cid = cs.get("id") or ""
            if not cid or cid in hidden:
                continue
            entries.append({
                "key":     cid,
                "label":   cs.get("name") or cid,
                "kind":    KIND_CUSTOM,
                "summary": _first_line(cs.get("description") or "") or (cs.get("name") or cid),
            })
    except Exception as exc:
        log.warning("strategy catalogue: custom strategies unavailable: %s", exc)

    try:
        for tpl in ea_templates.list_ea_templates():
            name = tpl.get("name") or ""
            if not name:
                continue
            entries.append({
                "key":     ea_templates.override_for_template(name),
                "label":   f"Template: {name}",
                "kind":    KIND_TEMPLATE,
                "summary": _template_summary(tpl),
            })
    except Exception as exc:
        log.warning("strategy catalogue: EA templates unavailable: %s", exc)

    return entries


def valid_keys(entries: list[dict]) -> list[str]:
    return [e["key"] for e in entries]


def prompt_lines(entries: list[dict]) -> list[str]:
    """The catalogue as prompt text, grouped so the model can tell a
    pre-coded strategy from a template the user built themselves."""
    groups = (
        (KIND_BUILTIN,  "Built-in strategies (Python-managed):"),
        (KIND_CUSTOM,   "Custom strategies (user-defined variants):"),
        (KIND_TEMPLATE, "EA templates (user-built, EA-managed — these replace "
                        "strategy dispatch entirely; recommend one by its exact "
                        '"template:<name>" key):'),
    )
    lines: list[str] = [
        "Available strategies — recommend exactly one, using its exact key:",
    ]
    for kind, heading in groups:
        rows = [e for e in entries if e["kind"] == kind]
        if not rows:
            continue
        lines.append(f"  {heading}")
        for e in rows:
            lines.append(f"    {e['key']}: {e['label']} — {e['summary']}")
    return lines


def resolve_key(raw: str | None, entries: list[dict]) -> str | None:
    """Map whatever the model returned onto a real catalogue key.

    Exact key first, then a case-insensitive match on key, label, or a bare
    template name (the model is given "template:Sniper" but writes "Sniper"
    often enough to be worth handling). Returns None when nothing matches,
    so the caller can decide its own fallback rather than silently trading a
    key that does not exist.
    """
    from forex_trader.core.core_ea_templates import TEMPLATE_OVERRIDE_PREFIX

    if not raw:
        return None
    raw = str(raw).strip()
    by_key = {e["key"]: e for e in entries}
    if raw in by_key:
        return raw

    low = raw.lower()
    for e in entries:
        if e["key"].lower() == low or e["label"].lower() == low:
            return e["key"]
    for e in entries:
        if e["kind"] != KIND_TEMPLATE:
            continue
        name = e["key"][len(TEMPLATE_OVERRIDE_PREFIX):]
        if name.lower() == low or f"template: {name}".lower() == low:
            return e["key"]
    return None


def describe(key: str, entries: list[dict] | None = None) -> tuple[str, str]:
    """(label, summary) for display. Falls back to the raw key so an
    unrecognised value still renders as itself rather than as blank."""
    for e in entries or []:
        if e["key"] == key:
            return e["label"], e["summary"]
    return key, ""
