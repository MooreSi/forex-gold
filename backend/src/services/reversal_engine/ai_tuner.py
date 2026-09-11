"""Letting the configured AI set the engine's capability switches.

Owner request, 2026-09-11: a button that has the AI read the market and set
the parameters itself, re-analysing every fifteen minutes, plus a one-shot
Recommend that puts the ML evidence and the AI together.

**This writes live trading settings from a model's free-text output.** Most
of what follows is therefore refusal rather than capability:

  * `TUNABLE` is a fixed allowlist. Anything else the model returns is
    dropped without comment. **Sizing is not on it and never will be by
    this route** -- an AI adjusting position size every fifteen minutes
    with no human in the loop is the worst thing this feature could do --
    and neither is live execution.
  * Every number is clamped to the same range the form allows a human.
  * Output that cannot be parsed changes nothing.
  * A refusal list covering every level type is rejected: a model that
    decides nothing is tradeable has not tuned the engine, it has switched
    it off, and it must not be able to do that through a settings field.
  * Nothing here raises into the engine's loop.

The AI is given MEASURED evidence -- the fitted barriers, the per-cohort
attribution, the measured execution cost, the meta-labeller's own verdict
on itself -- not just a price. That is the "ML" half of Recommend: the
numbers come from `market/barrier_fit`, `reversal_engine/attribution` and
`broker/tca`, and the model's job is to weigh them, not to invent them.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from backend.src.services.ai import provider as ai_provider

log = logging.getLogger("reversal_engine")

# key -> ("bool" | ("float", lo, hi) | "types")
TUNABLE: dict = {
    "re_atr_barriers_enabled":        "bool",
    "re_atr_stop_mult":               ("float", 0.2, 5.0),
    "re_atr_tp1_mult":                ("float", 0.2, 10.0),
    "entry_trigger_enabled":          "bool",
    "entry_trigger_rejection":        "bool",
    "entry_trigger_deceleration":     "bool",
    "entry_trigger_max_range_ratio":  ("float", 0.1, 3.0),
    "meta_label_gate_enabled":        "bool",
    "meta_label_threshold":           ("float", 0.0, 1.0),
    "session_liquidity_gate_enabled": "bool",
    "event_tier_gate_enabled":        "bool",
    "liquidity_map_levels_enabled":   "bool",
    "re_blocked_level_types":         "types",
}

_TRUE = ("1", "true", "yes", "on", "y")

_SYSTEM = (
    "You tune one XAUUSD signal engine. You are given measured evidence "
    "from its own trade history, not opinions. Reply with a single JSON "
    "object: {\"settings\": {...}, \"rationale\": \"one or two sentences\"}. "
    "Only include a setting you actually want changed. Never suggest "
    "anything about position size or live execution; you cannot change "
    "those and asking wastes the response. Prefer changing one thing at a "
    "time, and prefer changing nothing to guessing."
)


def _as_bool(value) -> Optional[int]:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    if isinstance(value, str):
        return 1 if value.strip().lower() in _TRUE else 0
    return None


def sanitise(settings: Optional[dict]) -> dict:
    """The subset of `settings` this module is willing to write."""
    out: dict = {}
    for key, raw in (settings or {}).items():
        spec = TUNABLE.get(key)
        if spec is None:
            continue

        if spec == "bool":
            v = _as_bool(raw)
            if v is not None:
                out[key] = v
            continue

        if spec == "types":
            from backend.src.services.risk import capability_gates as cg
            known = {t.lower() for t in cg.KNOWN_LEVEL_TYPES}
            if isinstance(raw, str):
                raw = raw.split(",")
            if not isinstance(raw, (list, tuple)):
                continue
            picked = [str(t).strip().lower() for t in raw
                      if str(t).strip().lower() in known]
            # Refusing everything is switching the engine off, not tuning it.
            if picked and set(picked) >= known:
                log.warning("[RE-AI] refused a block list covering every "
                            "level type -- that is a stop, not a setting")
                continue
            out[key] = ",".join(sorted(set(picked)))
            continue

        _kind, lo, hi = spec
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v != v:                      # NaN
            continue
        out[key] = max(lo, min(hi, v))
    return out


def parse_response(raw: str) -> dict:
    """`{settings, rationale, error}` from whatever the model actually said.

    Models wrap JSON in prose and fences. This digs out the first balanced
    object rather than trusting the response to be clean, and returns an
    explicit error instead of an empty recommendation so a broken provider
    cannot look like "no change needed".
    """
    text = (raw or "").strip()
    if not text:
        return {"settings": {}, "rationale": "", "error": "empty response"}

    candidate = text
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        candidate = fence.group(1).strip()
    else:
        brace = re.search(r"\{.*\}", text, re.S)
        if brace:
            candidate = brace.group(0)

    try:
        data = json.loads(candidate)
    except ValueError as e:
        return {"settings": {}, "rationale": text[:300],
                "error": f"could not read JSON from the response: {e}"}

    if not isinstance(data, dict):
        return {"settings": {}, "rationale": "", "error": "response was not an object"}

    settings = data.get("settings")
    return {"settings": settings if isinstance(settings, dict) else {},
            "rationale": str(data.get("rationale") or "")}


async def gather_evidence(bridge, rs: Optional[dict] = None) -> dict:
    """The measured picture handed to the model. Never raises.

    Everything here is already computed by something else. The point of
    the function is that the AI sees the same numbers a human would read
    off the Research card, rather than being asked to reason from a price.
    """
    from backend.src.services.market import barrier_fit
    from backend.src.services.reversal_engine import attribution, measure_repo
    from backend.src.services.reversal_engine import meta_label

    ev: dict = {"at": time.time()}

    try:
        obs = measure_repo.excursion_observations()
        fit = barrier_fit.fit_barriers(obs)
        ev["fitted_barriers"] = {
            "stop_atr_mult": fit.stop_atr_mult, "target_atr_mult": fit.target_atr_mult,
            "implied_rr": fit.rr, "n_winners": fit.n_winners,
            "refusal": fit.refusal,
        }
    except Exception as e:                        # noqa: BLE001
        ev["fitted_barriers"] = {"error": str(e)}

    try:
        rows = measure_repo.closed_executed_rows()
        table = attribution.cohorts(rows)
        ev["attribution"] = {
            axis: {k: {"n": c.n, "win_rate": round(c.win_rate, 3),
                       "mean_r": None if c.mean_r is None else round(c.mean_r, 3),
                       "net": round(c.net, 2)}
                   for k, c in groups.items()}
            for axis, groups in table.items()
        }
        ev["n_closed"] = len(rows)
    except Exception as e:                        # noqa: BLE001
        ev["attribution"] = {"error": str(e)}

    # The slow half of the evidence: how far trades actually travel, and
    # what the exit-policy sweep found. Read from the last study rather
    # than recomputed -- the sweep is hundreds of bridge round trips.
    #
    # Without these the model is asked where to put a target while being
    # shown only the fitted barriers, which is how the 2026-09-11 run
    # proposed a 2.0x ATR target that neither the reach data nor the sweep
    # supports. It reasoned correctly from half a picture.
    try:
        from backend.src.services.reversal_engine import research_lab
        summary = research_lab.last_summary()
        ev["reach"] = summary.get("reach") or {}
        ev["exit_policy_sweep"] = summary.get("sweep") or []
        ev["breakeven_penalty"] = summary.get("breakeven_penalty") or {}
        ev["study_ran_at"] = summary.get("ran_at")
        if not summary:
            ev["study_note"] = ("no research study has been run, so the reach "
                                "distribution and the exit-policy sweep are "
                                "unavailable. Recommend a target only if the "
                                "evidence you do have supports it.")
    except Exception as e:                        # noqa: BLE001
        ev["reach"] = {"error": str(e)}

    try:
        from backend.src.services.broker import tca_repo
        mean_r, n = tca_repo.mean_cost_r()
        ev["execution_cost"] = {"mean_cost_r": mean_r, "n": n}
    except Exception as e:                        # noqa: BLE001
        ev["execution_cost"] = {"error": str(e)}

    try:
        st = meta_label.get_instance().status
        ev["meta_labeller"] = {"ready": st.ready, "auc_oos": st.auc_oos,
                               "n_samples": st.n_samples, "refusal": st.refusal}
    except Exception as e:                        # noqa: BLE001
        ev["meta_labeller"] = {"error": str(e)}

    if bridge is not None:
        try:
            from backend.src.services.reversal_engine import level_detector as ld
            from backend.src.services.market import order_flow
            h1 = await bridge.get_candles("H1", 60)
            tick = await bridge.get_tick()
            ev["market"] = {
                "price": getattr(tick, "mid", None) or (tick or {}).get("mid"),
                "htf_bias": ld.get_htf_bias(h1 or []),
            }
            ticks = await bridge.get_ticks_range(time.time() - 900, time.time())
            spread = order_flow.spread_stats(ticks or [])
            ev["market"]["spread_pts"] = spread.mean_pts
            ev["market"]["spread_widening_ratio"] = spread.widening_ratio
        except Exception as e:                    # noqa: BLE001
            ev["market"] = {"error": str(e)}

    ev["current_settings"] = {k: (rs or {}).get(k) for k in TUNABLE}
    return ev


def _build_prompt(evidence: dict) -> str:
    return (
        "Measured evidence for the XAUUSD reversal engine:\n\n"
        + json.dumps(evidence, indent=2, default=str)
        + "\n\nThe settings you may change, with their allowed ranges:\n"
        + json.dumps({k: (v if isinstance(v, str) else list(v))
                      for k, v in TUNABLE.items()}, indent=2)
        + "\n\nNotes you need:\n"
        "- mean_r is per-trade R. Negative means that cohort loses money.\n"
        "- A cohort with a small n is not evidence; say so rather than acting.\n"
        "- meta_label_gate_enabled does nothing while the meta-labeller "
        "reports ready=false.\n"
        "- re_blocked_level_types refuses a level type outright. Use it only "
        "where the attribution is clear and n is large.\n"
        "- `reach` is how far trades ACTUALLY travel in R, all trades, not "
        "just winners. A target above the median reach is a target most "
        "trades never see, whatever the fitted barriers imply.\n"
        "- `fitted_barriers` is a DIAGNOSIS, not a target. A stop far wider "
        "than the target means winners go a long way against before working "
        "and then barely travel; it says the entries are wrong, and it is "
        "not a stop width to adopt.\n"
        "- `exit_policy_sweep` is measured expectancy per stop/target pair, "
        "in POINTS, net of cost, best first. A row whose ci_low and ci_high "
        "straddle zero is not evidence; say so rather than acting on it.\n"
        "- Where the reach data, the sweep and the fit disagree, prefer "
        "changing nothing and say which two disagree.\n"
    )


async def recommend(bridge, rs: Optional[dict] = None) -> dict:
    """Evidence plus the configured AI, as a recommendation. Writes nothing."""
    import backend.src.config as _cfg_mod

    evidence = await gather_evidence(bridge, rs)
    cfg = {
        "ai_provider":       _cfg_mod.get("ai_provider", "claude"),
        "anthropic_api_key": _cfg_mod.get("anthropic_api_key", ""),
        "claude_model":      _cfg_mod.get("claude_model", ""),
        "deepseek_api_key":  _cfg_mod.get("deepseek_api_key", ""),
        "deepseek_model":    _cfg_mod.get("deepseek_model", ""),
    }
    if not ai_provider.is_configured(cfg):
        return {"settings": {}, "rationale": "", "evidence": evidence,
                "error": "no AI provider is configured (Settings > AI)"}

    raw = await ai_provider.complete(cfg, _SYSTEM, _build_prompt(evidence),
                                     max_tokens=700, timeout=45)
    parsed = parse_response(raw)
    parsed["settings"] = sanitise(parsed.get("settings"))
    parsed["evidence"] = evidence
    return parsed


def _write_settings(settings: dict) -> None:
    from backend.src.db import database as db_module
    db_module.update_risk_settings(settings)


async def auto_tune(bridge, rs: Optional[dict] = None) -> dict:
    """The fifteen-minute pass. Off unless `re_ai_tuning_enabled` is set.

    Never raises: this runs on the engine's own loop, and a provider
    outage must not take the engine down with it.
    """
    rs = rs or {}
    if not rs.get("re_ai_tuning_enabled"):
        return {"skipped": "off"}
    try:
        rec = await recommend(bridge, rs)
    except Exception as e:                        # noqa: BLE001
        log.warning("[RE-AI] auto-tune failed: %s", e)
        return {"error": str(e)}

    settings = sanitise(rec.get("settings"))
    if not settings:
        return {"applied": {}, "rationale": rec.get("rationale", ""),
                "error": rec.get("error", "")}

    _write_settings(settings)
    log.info("[RE-AI] auto-tune applied %s -- %s", settings,
             rec.get("rationale", "")[:200])
    return {"applied": settings, "rationale": rec.get("rationale", "")}


async def tune_once(bridge) -> dict:
    """One pass, reading the switch itself. What the engine's loop calls.

    The settings read lives here rather than in the loop so the engine
    stays an orchestrator: it decides WHEN, this decides whether and what.
    """
    try:
        from backend.src.db import database as db_module
        rs = db_module.get_risk_settings() or {}
    except Exception as e:                        # noqa: BLE001
        return {"error": f"could not read settings: {e}"}
    return await auto_tune(bridge, rs)
