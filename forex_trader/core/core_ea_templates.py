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

from forex_trader.core import database as db_module

log = logging.getLogger(__name__)

TEMPLATE_OVERRIDE_PREFIX = "template:"

MODE_CHOICES       = ("single", "grid")
TPSL_MODE_CHOICES   = ("off", "on", "stealth")
ANCHOR_CHOICES      = ("unified", "distributed")
TRAIL_MODE_CHOICES  = ("off", "candle", "step", "fractal", "tp")
BE_MODE_CHOICES     = ("entry", "entry_buffer")

# TP ladder depth. Was 8; raised to 10 (2026-07-29) to match the copier
# EA's own InpC{n}_TP1..TP10 / Pct1..Pct10 inputs, so a template can
# express the same ladder without truncation.
MAX_TP_LEVELS = 10

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
    "group_tp_action":   False,
    "sig_guard":         False,

    # ── Entries & lots (2026-07-29) ──────────────────────────────────
    # The copier splits what this module previously collapsed into a
    # single `grid_legs`: an ANCHOR leg that enters at/near market and
    # PENDING legs that rest in the zone, each with their own count and
    # lot. Observed live -- on signal 25202 the copier opened
    # "C2_..._ANC" at 4026 (market, just outside the zone) and
    # "C2_..._PEN" at 4025 (limit, at the zone edge), both 0.01.
    # grid_legs is kept for backward compatibility with existing rows.
    "anchors":           1,
    "pendings":          1,
    "lot_anchor":        0.01,
    "lot_pending":       0.01,
    # Template-level stop, in pips, used when the signal has no usable SL
    # (the signal's own SL still wins -- see the TP note below).
    "sl_pips":           50.0,
    # 0 = OFF -> fall back to the app's own risk_per_trade_pct sizing.
    "risk_pct":          0.0,
    # 0 = OFF. Close everything on this channel if floating loss exceeds
    # this many account-currency units (copier's EQUITY PROTECT ($)).
    "equity_protect":    0.0,
    # Reject a signal that arrives this many pips beyond its own zone --
    # copier's InpC{n}_LateGuardPips. 0 = no guard.
    "late_guard_pips":   0.0,
    # Trim the anchor's entry back toward the zone rather than chasing
    # (copier's InpC{n}_AncShave).
    "anc_shave":         True,
    # Push the signal's SL to the broker rather than tracking internally.
    "auto_sl":           True,
    # Partial closes at each TP; False = single close at the final level.
    "partials":          True,
    # Which TP level cancels still-resting siblings. 0 = never. Supersedes
    # the older boolean `cancel_pending` (kept for existing rows), which
    # could only say "on the first fill".
    "cancel_pending_level": 0,
    # Trailing geometry, all pips (copier's InpTrail* globals). Only read
    # when trail_mode != "off".
    "trail_distance":    50.0,
    "trail_step":        10.0,
    "trail_activation":  100.0,
    "trail_padding":     0.0,
    # Execution guards.
    "max_spread_pips":   6.0,
    "slippage":          20,
    # Harvest trigger in pips (distinct from harvest_threshold, which is
    # in account currency).
    "harvest_pips":      1.0,
    # Ignore a Telegram signal older than this at fill time.
    "signal_max_age_sec": 10,

    # ── Anchor TP ladder ─────────────────────────────────────────────
    **{f"tp{n}_pips": 0.0 for n in range(1, MAX_TP_LEVELS + 1)},
    **{f"tp{n}_pct":  0.0 for n in range(1, MAX_TP_LEVELS + 1)},

    # ── Pending TP ladder (2026-07-29) ───────────────────────────────
    # A SEPARATE ladder for the resting legs. The copier ships wider
    # defaults here than for the anchor (40/70/110/150/250 vs
    # 30/50/80/100/130) on the logic that a leg filled deeper in the zone
    # has more room to the same structural target -- confirmed live, its
    # pending leg on signal 25204 entered 1pt better and so carried 14pt
    # of reward against the anchor's 13pt. Kept as its own table rather
    # than derived, so the two can be tuned independently.
    **{f"tp_pen{n}_pips": 0.0 for n in range(1, MAX_TP_LEVELS + 1)},
    **{f"tp_pen{n}_pct":  0.0 for n in range(1, MAX_TP_LEVELS + 1)},
}

# Anchor TP (2026-07-24) -- tp{n}_pips is a fallback (entry ± N pips, used
# only when the signal itself didn't supply that TP level); tp{n}_pct always
# wins over the signal, which never states a per-level close percentage.
# See core_open_trade.py's EA-handoff block for how these get resolved into
# the final tp{n}/pct{n} values sent to the EA.
_ANCHOR_TP_FIELDS = (
    tuple(f"tp{n}_pips" for n in range(1, MAX_TP_LEVELS + 1))
    + tuple(f"tp{n}_pct" for n in range(1, MAX_TP_LEVELS + 1))
)
_PENDING_TP_FIELDS = (
    tuple(f"tp_pen{n}_pips" for n in range(1, MAX_TP_LEVELS + 1))
    + tuple(f"tp_pen{n}_pct" for n in range(1, MAX_TP_LEVELS + 1))
)

# Group TP Action (2026-07-28) -- grid mode only: the FIRST TP any leg of
# the group clears cancels every other still-resting sibling (same
# mechanism cancel_pending already uses on a fill, just triggered by a TP
# hit instead) and moves every other already-live sibling's SL to its own
# breakeven. Treats one leg's TP as validation of the whole basket rather
# than leaving unfilled legs to fill blind or open siblings sitting at
# their original, wider stop. See ForexTraderBridge.mq5's
# ApplyGroupTpAction. No effect in single mode (no siblings to act on).
_BOOL_FIELDS  = (
    "tg_cmd_enabled", "harvest_enabled", "cancel_pending", "group_tp_action",
    "sig_guard", "anc_shave", "auto_sl", "partials",
)
_FLOAT_FIELDS = (
    "harvest_threshold", "grid_step_pts", "be_buffer_pts",
    "lot_anchor", "lot_pending", "sl_pips", "risk_pct", "equity_protect",
    "late_guard_pips", "trail_distance", "trail_step", "trail_activation",
    "trail_padding", "max_spread_pips", "harvest_pips",
) + _ANCHOR_TP_FIELDS + _PENDING_TP_FIELDS
_INT_FIELDS   = (
    "grid_legs", "be_trigger", "anchors", "pendings",
    "cancel_pending_level", "slippage", "signal_max_age_sec",
)
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
    merged["be_trigger"] = max(1, min(MAX_TP_LEVELS, merged["be_trigger"]))
    # 0 is meaningful here ("never cancel"), so the floor is 0 not 1.
    merged["cancel_pending_level"] = max(
        0, min(MAX_TP_LEVELS, merged["cancel_pending_level"]))
    # Counts and lots must be sane before they reach the EA -- a 0-lot or
    # negative-count leg is rejected by the broker, and a template is
    # edited by hand often enough that this is worth enforcing here
    # rather than discovering it at order-send time.
    merged["anchors"]  = max(0, min(20, merged["anchors"]))
    merged["pendings"] = max(0, min(20, merged["pendings"]))
    for f in ("lot_anchor", "lot_pending"):
        merged[f] = max(0.0, merged[f])
    for f in ("sl_pips", "risk_pct", "equity_protect", "late_guard_pips",
              "trail_distance", "trail_step", "trail_activation",
              "trail_padding", "max_spread_pips", "harvest_pips"):
        merged[f] = max(0.0, merged[f])
    merged["slippage"] = max(0, merged["slippage"])
    merged["signal_max_age_sec"] = max(0, merged["signal_max_age_sec"])
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
