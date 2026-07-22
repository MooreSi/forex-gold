"""Live-tunable per-strategy point/multiplier parameters -- lets Conservative,
Scalp Runner, GD VIP Runner, Adaptive Runner, and Adaptive Runner 2's fixed
SL/TP constants be edited from Trading > Strategy without a code change or
restart, plus a small named-template library to save/reapply parameter sets.

Inspired by a third-party EA's "Settings Templates" panel (2026-07-22
investigation) -- Python-only by design, no MQL5 involved: every strategy
covered here is already fully resolved to concrete SL/TP price levels by
Python before the EA (if any) ever sees the trade, so there's no EA-side
constant to retune to get the same "no recompile" effect.

Scope: SL-shaping constants only (a strategy's fixed point distance, or its
widen/cap/floor multipliers) -- not the TP-ladder close-percentage tables
(_CLIMBER_PCTS/_GDVR_PCTS in core_open_trade.py), which are shared across
several strategies and shaped as an 8-TP-count lookup table rather than a
single tunable value; retuning those is a materially bigger, separate task.

Not synced between Local/Remote nodes (unlike risk settings) -- a template
applied on one node does not propagate to its pair. Revisit if that turns
out to matter in practice.
"""
from __future__ import annotations

import json
import logging
import time

from forex_trader.core import database as db_module
from forex_trader.core.models import (
    STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER, STRATEGY_GD_VIP_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2,
)

log = logging.getLogger(__name__)

# (param_key, label, default, unit) -- unit is display-only ("pt"/"x"/"frac").
PARAM_SPECS: dict[str, list[tuple[str, str, float, str]]] = {
    STRATEGY_CONSERVATIVE: [
        ("sl_pt",  "Stop Loss", 5.0, "pt"),
        ("tp1_pt", "TP1",       3.0, "pt"),
    ],
    STRATEGY_SCALP_RUNNER: [
        ("sl_pt",  "Stop Loss", 10.0, "pt"),
        ("tp1_pt", "TP1",       3.0,  "pt"),
        ("tp2_pt", "TP2",       4.0,  "pt"),
    ],
    STRATEGY_GD_VIP_RUNNER: [
        ("sl_mult",     "SL widen multiplier", 4.0,  "x"),
        ("sl_cap_pt",   "SL widen cap",        20.0, "pt"),
        ("sl_floor_pt", "SL floor (bad data)", 8.0,  "pt"),
    ],
    STRATEGY_ADAPTIVE_RUNNER: [
        ("sl_mult",     "SL widen multiplier",   4.0,  "x"),
        ("sl_cap_pt",   "SL widen cap",          20.0, "pt"),
        ("sl_floor_pt", "SL floor (bad data)",   8.0,  "pt"),
        ("tp_cap_frac", "Final-TP distance cap", 0.50, "frac"),
    ],
    STRATEGY_ADAPTIVE_RUNNER_2: [
        ("sl_pt", "Stop Loss (fixed)", 10.0, "pt"),
    ],
}

STRATEGY_LABELS: dict[str, str] = {
    STRATEGY_CONSERVATIVE:      "Conservative",
    STRATEGY_SCALP_RUNNER:      "Scalp Runner",
    STRATEGY_GD_VIP_RUNNER:     "GD VIP Runner",
    STRATEGY_ADAPTIVE_RUNNER:   "Adaptive Runner",
    STRATEGY_ADAPTIVE_RUNNER_2: "Adaptive Runner 2",
}

# Strategies in display order for the UI's strategy picker.
PARAM_STRATEGIES: list[str] = [
    STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER, STRATEGY_GD_VIP_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2,
]

_DEFAULTS: dict[str, dict[str, float]] = {
    strategy: {key: default for key, _label, default, _unit in specs}
    for strategy, specs in PARAM_SPECS.items()
}

_CACHE_TTL = 10.0  # seconds -- these change only on user edit
_cache: dict[str, tuple[dict, float]] = {}


def default_params(strategy: str) -> dict[str, float]:
    return dict(_DEFAULTS.get(strategy, {}))


def get_strategy_params(strategy: str) -> dict[str, float]:
    """Live values for `strategy` -- DB override merged over the built-in
    defaults, so a param never silently disappears if get_app_config()
    fails, or a previously-saved override predates a newly-added param."""
    now = time.time()
    cached = _cache.get(strategy)
    if cached is not None and (now - cached[1]) < _CACHE_TTL:
        return cached[0]
    values = default_params(strategy)
    raw = db_module.get_app_config(f"strategy_params_{strategy}")
    if raw:
        try:
            override = json.loads(raw)
            values.update({k: float(v) for k, v in override.items() if k in values})
        except Exception as exc:
            log.debug("[StrategyParams] bad override for %s: %s", strategy, exc)
    _cache[strategy] = (values, now)
    return values


def set_strategy_params(strategy: str, params: dict) -> dict:
    """Overwrite the live values for `strategy`. Unknown keys in `params`
    are silently dropped rather than stored -- a typo'd key would
    otherwise be saved but never actually read by anything."""
    if strategy not in _DEFAULTS:
        raise ValueError(f"Unknown strategy: {strategy}")
    valid_keys = set(_DEFAULTS[strategy])
    merged = default_params(strategy)
    merged.update({k: float(v) for k, v in params.items() if k in valid_keys})
    db_module.set_app_config(f"strategy_params_{strategy}", json.dumps(merged))
    _cache.pop(strategy, None)
    return merged


def reset_strategy_params(strategy: str) -> dict:
    """Delete the live override -- reverts to built-in defaults."""
    if strategy not in _DEFAULTS:
        raise ValueError(f"Unknown strategy: {strategy}")
    defaults = default_params(strategy)
    db_module.set_app_config(f"strategy_params_{strategy}", json.dumps(defaults))
    _cache.pop(strategy, None)
    return defaults


# ── Named template library ───────────────────────────────────────────────────

def list_templates(strategy: str) -> list[dict]:
    with db_module.db() as conn:
        rows = conn.execute(
            "SELECT id, strategy, name, params_json, created_at FROM strategy_param_templates "
            "WHERE strategy=? ORDER BY created_at DESC", (strategy,),
        ).fetchall()
    out = []
    for r in rows:
        d = db_module.row_to_dict(r)
        try:
            d["params"] = json.loads(d.pop("params_json"))
        except Exception:
            d["params"] = {}
        out.append(d)
    return out


def save_template(strategy: str, name: str, params: dict) -> int:
    if strategy not in _DEFAULTS:
        raise ValueError(f"Unknown strategy: {strategy}")
    name = (name or "").strip()
    if not name:
        raise ValueError("Template name is required")
    valid_keys = set(_DEFAULTS[strategy])
    clean = {k: float(v) for k, v in params.items() if k in valid_keys}
    with db_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO strategy_param_templates (strategy, name, params_json, created_at) "
            "VALUES (?,?,?,?)",
            (strategy, name, json.dumps(clean), time.time()),
        )
        return cur.lastrowid


def delete_template(template_id: int) -> None:
    with db_module.db() as conn:
        conn.execute("DELETE FROM strategy_param_templates WHERE id=?", (template_id,))


def apply_template(template_id: int) -> dict:
    """Load a saved template and make it the strategy's live values."""
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT strategy, params_json FROM strategy_param_templates WHERE id=?",
            (template_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"Template {template_id} not found")
    strategy, params_json = row[0], row[1]
    try:
        params = json.loads(params_json)
    except Exception:
        params = {}
    return set_strategy_params(strategy, params)
