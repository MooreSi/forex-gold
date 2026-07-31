"""EA-native trade-management templates (2026-07-23) -- Trading > Strategy
Parameters > Templates section.

A template is a complete, self-contained, EA-managed trade-management
definition (Grid vs Single entry, TP/SL visibility, trailing method,
breakeven rule, cancel-pending-siblings, profit harvesting) that a channel
can be assigned to in place of a built-in strategy -- see
core_signal_resolution.py's handling of a "template:<name>" channel
override. Every field is sent fresh to the EA on each open_trade/
place_pending_order call (ea_bridge.py's `template` payload), so changing
a template's values never needs an EA recompile.

Modelled on core_strategy_params.py's conventions (get/set/cache shape),
but this is a genuinely different concept -- that module holds named
presets of ONE existing Python strategy's numeric knobs; this module holds
complete alternative management definitions that bypass Python strategy
dispatch entirely once assigned to a channel.
"""
from __future__ import annotations

import logging
import time

from backend.src.db import database as db_module

log = logging.getLogger(__name__)

TEMPLATE_OVERRIDE_PREFIX = "template:"

MODE_CHOICES       = ("single", "grid")
TPSL_MODE_CHOICES   = ("off", "on", "stealth")
ANCHOR_CHOICES      = ("unified", "distributed")
TRAIL_MODE_CHOICES  = ("off", "candle", "step", "fractal", "tp")
BE_MODE_CHOICES     = ("entry", "entry_buffer")

DEFAULTS: dict = {
    "tg_cmd_enabled":    True,
    "harvest_enabled":   False,
    "harvest_threshold": 50.0,
    "mode":              "single",
    "grid_step_pts":     10.0,
    "grid_legs":         3,
    "tpsl_mode":         "on",
    "anchor":            "unified",
    "trail_mode":        "off",
    "be_mode":           "entry",
    "be_buffer_pts":     1.0,
    "be_trigger":        1,
    "cancel_pending":    False,
    "sig_guard":         False,
    **{f"tp{n}_pips": 0.0 for n in range(1, 9)},
    **{f"tp{n}_pct":  0.0 for n in range(1, 9)},
}

# Anchor TP (2026-07-24) -- tp{n}_pips is a fallback (entry ± N pips, used
# only when the signal itself didn't supply that TP level); tp{n}_pct always
# wins over the signal, which never states a per-level close percentage.
# See core_open_trade.py's EA-handoff block for how these get resolved into
# the final tp{n}/pct{n} values sent to the EA.
_ANCHOR_TP_FIELDS = tuple(f"tp{n}_pips" for n in range(1, 9)) + tuple(f"tp{n}_pct" for n in range(1, 9))

_BOOL_FIELDS  = ("tg_cmd_enabled", "harvest_enabled", "cancel_pending", "sig_guard")
_FLOAT_FIELDS = ("harvest_threshold", "grid_step_pts", "be_buffer_pts") + _ANCHOR_TP_FIELDS
_INT_FIELDS   = ("grid_legs", "be_trigger")
_STR_FIELDS   = ("mode", "tpsl_mode", "anchor", "trail_mode", "be_mode")

_CHOICES = {
    "mode": MODE_CHOICES, "tpsl_mode": TPSL_MODE_CHOICES,
    "anchor": ANCHOR_CHOICES, "trail_mode": TRAIL_MODE_CHOICES,
    "be_mode": BE_MODE_CHOICES,
}


def is_template_override(override: str | None) -> bool:
    return bool(override) and override.startswith(TEMPLATE_OVERRIDE_PREFIX)


def template_name_from_override(override: str) -> str:
    return override[len(TEMPLATE_OVERRIDE_PREFIX):]


def override_for_template(name: str) -> str:
    return f"{TEMPLATE_OVERRIDE_PREFIX}{name}"


def _row_to_template(row) -> dict:
    d = db_module.row_to_dict(row)
    for f in _BOOL_FIELDS:
        d[f] = bool(d.get(f))
    for f in _INT_FIELDS:
        d[f] = int(d.get(f))
    for f in _FLOAT_FIELDS:
        d[f] = float(d.get(f))
    return d


def list_ea_templates() -> list[dict]:
    with db_module.db() as conn:
        rows = conn.execute(
            "SELECT * FROM ea_trade_templates ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_row_to_template(r) for r in rows]


def get_ea_template(name: str) -> dict | None:
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT * FROM ea_trade_templates WHERE name=?", (name,),
        ).fetchone()
    return _row_to_template(row) if row else None


def _clean_fields(fields: dict) -> dict:
    """Merge `fields` over DEFAULTS, coercing types and rejecting unknown
    enum values -- unknown/extra keys are silently dropped rather than
    stored, same convention as core_strategy_params.set_strategy_params."""
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in fields.items() if k in DEFAULTS})
    for f in _BOOL_FIELDS:
        merged[f] = bool(merged[f])
    for f in _INT_FIELDS:
        merged[f] = int(merged[f])
    for f in _FLOAT_FIELDS:
        merged[f] = float(merged[f])
    for f, choices in _CHOICES.items():
        if merged[f] not in choices:
            raise ValueError(f"Invalid {f}: {merged[f]!r} (must be one of {choices})")
    merged["be_trigger"] = max(1, min(8, merged["be_trigger"]))
    return merged


def save_ea_template(name: str, fields: dict) -> dict:
    """Insert or overwrite the named template."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Template name is required")
    clean = _clean_fields(fields)
    now = time.time()
    with db_module.db() as conn:
        existing = conn.execute(
            "SELECT created_at FROM ea_trade_templates WHERE name=?", (name,),
        ).fetchone()
        created_at = existing[0] if existing else now
        cols = list(clean.keys())
        conn.execute(
            f"INSERT INTO ea_trade_templates (name, {', '.join(cols)}, created_at, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in cols)}, ?, ?) "
            f"ON CONFLICT(name) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols)
            + ", updated_at=excluded.updated_at",
            (name, *[clean[c] for c in cols], created_at, now),
        )
    log.info("[EATemplates] saved template %r: %s", name, clean)
    return get_ea_template(name)


def delete_ea_template(name: str) -> None:
    with db_module.db() as conn:
        conn.execute("DELETE FROM ea_trade_templates WHERE name=?", (name,))
    log.info("[EATemplates] deleted template %r", name)
