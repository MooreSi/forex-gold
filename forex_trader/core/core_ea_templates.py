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

import json
import logging
import time

from forex_trader.core import database as db_module

log = logging.getLogger(__name__)

TEMPLATE_OVERRIDE_PREFIX = "template:"

MODE_CHOICES       = ("single", "grid")
TPSL_MODE_CHOICES   = ("off", "on", "stealth")
ANCHOR_CHOICES      = ("unified", "distributed")
TRAIL_MODE_CHOICES  = ("off", "candle", "step", "fractal", "tp", "staged")

# Number of ratchet rungs "staged" trail_mode supports (2026-08-10). Each
# rung fires once when floating profit crosses its trigger_pips and moves
# SL to target_pips from entry (negative = still risking a loss, just a
# smaller one; 0 = breakeven; positive = a locked-in profit) -- unlike
# be_trigger/trail_mode="tp", the SL target here is independent of the
# trigger price, which is what lets a rung say "at +400 pips, lock +300"
# rather than only ever locking the exact price that armed it. The last
# rung can also strip the take-profit (remove_tp) so the position rides
# the trailing stop instead of being flattened at a fixed target. Once
# every configured rung (trigger_pips > 0) has fired, ManageTemplate falls
# through to the same trail_distance/trail_step step-trail every other
# trail_mode="step" template already uses, so "trail every N pips" beyond
# the ratchet needs no rung-specific code of its own.
SL_STAGE_COUNT = 3
BE_MODE_CHOICES     = ("entry", "entry_buffer")
# How grid mode places its resting legs (2026-08-04).
#   "zone" -- span the SIGNAL's own stated entry zone (zone_low/zone_high),
#             the behaviour since 2026-07-28 and the default so no existing
#             template changes on upgrade.
#   "step" -- ignore the zone; step grid_step_pts away from the anchor's own
#             base price, which is what the reference "sniper" copier does
#             (its panel calls this LADDER STEP).
# Why this is a choice rather than a fix: zone-spanning honours the levels
# the signal actually named, but it silently loses legs. A leg landing on
# the wrong side of the market is skipped by HandleOpenTemplateGrid, and
# since the anchor now always fires at market regardless of the zone
# (2026-08-03 always-fire directive), price has frequently already run past
# the zone by then -- so the anchor fills and the resting leg quietly never
# appears. Step mode is immune by construction: it is always measured away
# from current price, so it can never land on the wrong side.
PENDING_MODE_CHOICES = ("zone", "step")

# TP ladder depth. Briefly raised to 10 (2026-07-29) to match the copier
# EA's own InpC{n}_TP1..TP10 / Pct1..Pct10 inputs, then reverted back to 8
# the same day (explicit user directive) -- core_open_trade.py's EA-handoff
# block already only ever resolved levels 1-8 regardless, so this restores
# consistency between the schema/UI and what actually reaches the EA.
MAX_TP_LEVELS = 8

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
    # Sig Guard distance, in pips. The reference copier's panel shows this
    # as part of the guard itself ("SIG GUARD: 20p"), where ours was a bare
    # on/off. 0 keeps the original all-or-nothing behaviour (any open trade
    # on this channel in the same direction blocks); >0 narrows it to "only
    # block when the existing trade's entry is within this many pips", so a
    # genuinely separate setup further down the chart can still trade.
    "sig_guard_pips":    0.0,
    # See PENDING_MODE_CHOICES. "zone" preserves the behaviour of every
    # template saved before this field existed.
    "pending_mode":      "zone",

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
    # 0 = OFF. Mirror image of equity_protect: close everything on this
    # channel once the group's COMBINED floating PROFIT reaches this many
    # account-currency units. Added 2026-08-12 after a basket of "Staged
    # Ratchet 100-500" trades peaked at $1,210 combined floating profit
    # (each trade's own SL ratchet was managing its own risk correctly) but
    # gave most of it back to +$45.70 realized -- there was no mechanism to
    # lock in a strong combined swing across the whole group at once. See
    # core_equity_protect.check_basket_harvest, which groups and checks
    # this the same way check_equity_protect already does for the loss
    # side (same (tg_source, strategy) grouping, same close_trade_fn).
    "basket_harvest_threshold": 0.0,
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
    # When true (default -- matches every template saved before this field
    # existed), the last CONFIGURED Anchor TP level closes whatever remains
    # of the position outright, regardless of that level's own tp{n}_pct.
    # Set false to have the last level close only its own pct and leave the
    # remainder open, managed from there by Trail/BE -- e.g. a ladder whose
    # tp{n}_pct sum well under 100 and is meant to leave a genuine runner
    # rather than being flattened the moment the last defined level clears.
    "close_full_on_last": True,
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

    # ── Remaining goldbotea.set behaviour parity (2026-07-29) ────────
    # The copier keeps these as EA globals rather than per-channel, but
    # they change how a trade is placed and managed, so they belong on
    # the template -- that way two channels can differ, which the copier
    # itself cannot express.
    #
    # Dynamic ATR sizing of SL/TP1 (InpUseDynamicATR/InpATRPeriod/
    # InpATR_SL_Mult/InpATR_TP1_Mult). When on, sl_pips is ignored in
    # favour of ATR x atr_sl_mult.
    "use_dynamic_atr":   False,
    "atr_period":        14,
    "atr_sl_mult":       1.5,
    "atr_tp1_mult":      1.5,
    # Minimum distance to keep from price when placing/adjusting a stop
    # (InpDefaultGuardPips), and the hard floor the EA will never tighten
    # inside (InpSafetyCapPips). These are what stop a breakeven move
    # being rejected as an invalid stop -- the failure that cost a full
    # -$100 on ticket 1663956102.
    "guard_pips":        10.0,
    "safety_cap_pips":   10.0,
    # Backstop stop at emergency_sl_mult x the normal distance, in case
    # the primary stop is rejected or removed (InpUseEmergencySL).
    "use_emergency_sl":  False,
    "emergency_sl_mult": 2.0,
    # Reject a signal whose own TP1:SL ratio is below this
    # (InpSignalRRRatio). 0 = no filter.
    "signal_rr_ratio":   0.0,
    # Which TP level arms the automatic SL move (InpTP1_TriggerLevel).
    # Distinct from be_trigger: this one drives the trailing/step logic,
    # be_trigger drives the move to breakeven specifically.
    "tp1_trigger_level": 1,
    # How far a manual "push SL" command moves the stop
    # (InpManualSLPushPips).
    "manual_sl_push_pips": 10.0,
    # Gold quotes at half-pip granularity on some feeds; anchor entries
    # to the half pip when set (InpGoldHalfPipAnchor).
    "gold_half_pip_anchor": False,

    # ── TP source (2026-07-30) ───────────────────────────────────────
    # "Use TP Levels from Telegram", one per ladder. When set, that
    # ladder's tp{n}_pips column is ignored and the TP levels come from
    # the triggering Telegram message's own stated TP prices instead;
    # tp{n}_pct still governs how much closes at each level, since a
    # message states prices but never sizes.
    #
    # Telegram-only by design: the internal generators (Reversal, Breakout,
    # Bounce, ORB) have no message to read, so they always use the pips
    # columns regardless of this flag -- which is the whole point of
    # keeping the manual ladder editable while this is on. A Telegram
    # signal that arrives with no usable TP levels also falls back to the
    # pips columns rather than opening with no targets at all.
    "tp_from_telegram":     False,
    "tp_pen_from_telegram": False,

    # ── Anchor TP ladder ─────────────────────────────────────────────
    **{f"tp{n}_pips": 0.0 for n in range(1, MAX_TP_LEVELS + 1)},
    **{f"tp{n}_pct":  0.0 for n in range(1, MAX_TP_LEVELS + 1)},

    # ── Staged SL ratchet (2026-08-10) ───────────────────────────────
    # See SL_STAGE_COUNT above. 0 trigger_pips = that rung is unused (kept
    # so a template can define fewer than 3 rungs).
    **{f"sl_stage{n}_trigger_pips": 0.0 for n in range(1, SL_STAGE_COUNT + 1)},
    **{f"sl_stage{n}_target_pips":  0.0 for n in range(1, SL_STAGE_COUNT + 1)},
    **{f"sl_stage{n}_remove_tp":    False for n in range(1, SL_STAGE_COUNT + 1)},

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

# Anchor TP -- tp{n}_pips is AUTHORITATIVE (entry ± N pips from the actual
# fill), replacing the signal's own TP prices entirely rather than only
# filling gaps it left (changed 2026-07-29 -- was a signal-wins-with-
# template-fallback rule from 2026-07-24; an EA Template channel's targets
# now come from the template regardless of the triggering message's own
# levels, so the same channel behaves identically across message shapes).
# tp{n}_pct was always template-only, since a signal states TP prices but
# never how much to close at each one. See core_open_trade.py's EA-handoff
# block for how these get resolved into the final tp{n}/pct{n} values sent
# to the EA.
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
_SL_STAGE_TRIGGER_FIELDS = tuple(
    f"sl_stage{n}_trigger_pips" for n in range(1, SL_STAGE_COUNT + 1))
_SL_STAGE_TARGET_FIELDS = tuple(
    f"sl_stage{n}_target_pips" for n in range(1, SL_STAGE_COUNT + 1))
_SL_STAGE_REMOVE_TP_FIELDS = tuple(
    f"sl_stage{n}_remove_tp" for n in range(1, SL_STAGE_COUNT + 1))

_BOOL_FIELDS  = (
    "tg_cmd_enabled", "harvest_enabled", "cancel_pending", "group_tp_action",
    "sig_guard", "anc_shave", "auto_sl", "partials", "close_full_on_last",
    "use_dynamic_atr", "use_emergency_sl", "gold_half_pip_anchor",
    "tp_from_telegram", "tp_pen_from_telegram",
) + _SL_STAGE_REMOVE_TP_FIELDS
_FLOAT_FIELDS = (
    "harvest_threshold", "grid_step_pts", "be_buffer_pts",
    "lot_anchor", "lot_pending", "sl_pips", "risk_pct", "equity_protect",
    "basket_harvest_threshold",
    "late_guard_pips", "sig_guard_pips",
    "trail_distance", "trail_step", "trail_activation",
    "trail_padding", "max_spread_pips", "harvest_pips",
    "atr_sl_mult", "atr_tp1_mult", "guard_pips", "safety_cap_pips",
    "emergency_sl_mult", "signal_rr_ratio", "manual_sl_push_pips",
) + _ANCHOR_TP_FIELDS + _PENDING_TP_FIELDS + _SL_STAGE_TRIGGER_FIELDS + _SL_STAGE_TARGET_FIELDS
_INT_FIELDS   = (
    "grid_legs", "be_trigger", "anchors", "pendings",
    "cancel_pending_level", "slippage", "signal_max_age_sec",
    "atr_period", "tp1_trigger_level",
)
_STR_FIELDS   = ("mode", "tpsl_mode", "anchor", "trail_mode", "be_mode",
                 "pending_mode")

_CHOICES = {
    "mode": MODE_CHOICES, "tpsl_mode": TPSL_MODE_CHOICES,
    "anchor": ANCHOR_CHOICES, "trail_mode": TRAIL_MODE_CHOICES,
    "be_mode": BE_MODE_CHOICES, "pending_mode": PENDING_MODE_CHOICES,
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
    # A ui.number box NiceGUI's Anchor/Pending TP grid uses reports its value
    # as None while empty -- momentarily true for every keystroke that clears
    # a field before typing the next number, not just an abandoned edit.
    # Falling back to the schema default (0.0 for every pips/pct column, so
    # a field left blank simply reads as "unused") keeps a save mid-edit from
    # throwing instead of a bare float(None)/int(None) TypeError aborting the
    # whole template. Root-caused live 2026-07-31 editing Anchor TP pips.
    for f in _INT_FIELDS:
        merged[f] = int(DEFAULTS[f]) if merged[f] is None else int(merged[f])
    for f in _FLOAT_FIELDS:
        merged[f] = float(DEFAULTS[f]) if merged[f] is None else float(merged[f])
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
    for f in ("sl_pips", "risk_pct", "equity_protect", "basket_harvest_threshold",
              "late_guard_pips", "sig_guard_pips",
              "trail_distance", "trail_step", "trail_activation",
              "trail_padding", "max_spread_pips", "harvest_pips",
              "atr_sl_mult", "atr_tp1_mult", "guard_pips",
              "safety_cap_pips", "emergency_sl_mult", "signal_rr_ratio",
              "manual_sl_push_pips", *_SL_STAGE_TRIGGER_FIELDS):
        merged[f] = max(0.0, merged[f])
    merged["slippage"] = max(0, merged["slippage"])
    merged["signal_max_age_sec"] = max(0, merged["signal_max_age_sec"])
    merged["atr_period"] = max(1, merged["atr_period"])
    merged["tp1_trigger_level"] = max(1, min(MAX_TP_LEVELS, merged["tp1_trigger_level"]))
    return merged


def signal_has_usable_zone(entry_low, entry_high) -> bool:
    """Whether a signal states an entry zone the EA can stage legs across.

    Mirrors HandleOpenTemplateGrid's own `useZone` test exactly
    (zoneLow > 0 && zoneHigh > zoneLow) so Python and the EA can never
    disagree about whether a zone exists. A single stated price is not a
    zone: high == low fails this, as does a missing/zero level.
    """
    try:
        low, high = float(entry_low or 0.0), float(entry_high or 0.0)
    except (TypeError, ValueError):
        return False
    return low > 0.0 and high > low


def apply_market_anchor_for_zoneless_signal(template: dict,
                                            entry_low, entry_high) -> dict:
    """Give a pendings-only template one market leg when the signal has no zone.

    An Instant Market Entry signal states a single price, not a range -- it
    means "take this now". Handed to a template with anchors=0, nothing can
    execute it: the EA's anchor loop (`for a = 1; a <= anchors`) never runs, and
    with no usable zone the resting legs fall back to step-staging
    grid_step_pts away from the market, so they only fill if price happens to
    come back. Gold Diggers VIP ran exactly that pairing on 2026-08-17 -- 8
    signals, 8 grids staged, zero legs filled, every one expiring at $0 after
    its 60-minute life.

    So the first leg becomes a market fill and the pending count drops by one.
    Converting a leg rather than adding one is deliberate: total leg count, and
    therefore total exposure, is exactly what the template already specified.
    The rest of the tuned geometry -- lots, SL, both TP ladders, BE and trail --
    is untouched.

    Templates that already take an anchor, non-grid templates, and signals that
    do state a real zone are all returned unchanged.
    """
    if signal_has_usable_zone(entry_low, entry_high):
        return template
    if not template or str(template.get("mode") or "") != "grid":
        return template
    anchors  = int(template.get("anchors") or 0)
    pendings = int(template.get("pendings") or 0)
    if anchors > 0 or pendings < 1:
        return template
    adjusted = dict(template)
    adjusted["anchors"]  = 1
    adjusted["pendings"] = pendings - 1
    log.info(
        "[EATemplates] %r: signal states a single price (%s), not a zone — "
        "staging 1 market anchor + %d pending(s) instead of %d resting leg(s), "
        "which could only fill on a retrace that may never come",
        template.get("name") or "template", entry_low, pendings - 1, pendings,
    )
    return adjusted


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


# ── Import / export (2026-08-06) ─────────────────────────────────────────
# Templates are the one piece of app state worth moving between installs
# (and between users -- a working ladder/trail/guard combination is the
# result of a lot of live tuning). The file is plain JSON so it stays
# diffable and hand-editable, wrapped in an envelope so an import can tell
# a real template file from any other .json a file browser offers.
#
# Only `name` plus the DEFAULTS keys travel: created_at/updated_at are
# local bookkeeping, and every unknown key is dropped on the way in by
# _clean_fields, so a file written by an older or newer build imports
# cleanly -- missing fields fall back to DEFAULTS rather than failing.
EXPORT_FORMAT   = "forex_trader.ea_templates"
EXPORT_VERSION  = 1
EXPORT_EXTENSION = ".eatpl.json"


def export_filename(prefix: str = "ea_templates") -> str:
    """Default save-as name, timestamped so repeated exports don't collide."""
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}{EXPORT_EXTENSION}"


def export_templates(names: list[str] | None = None) -> str:
    """Serialise templates to the JSON export envelope.

    `names=None` exports every saved template (what the panel's Export
    button does -- "all of the EA templates"); pass a list to export a
    subset.
    """
    rows = list_ea_templates()
    if names is not None:
        wanted = {n.strip() for n in names}
        rows = [r for r in rows if r["name"] in wanted]
    payload = {
        "format":      EXPORT_FORMAT,
        "version":     EXPORT_VERSION,
        "exported_at": time.time(),
        "templates":   [
            {"name": r["name"], **{k: r.get(k, DEFAULTS[k]) for k in DEFAULTS}}
            for r in rows
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _parse_export(payload: bytes | str) -> list[dict]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    try:
        data = json.loads(payload)
    except Exception as exc:
        raise ValueError(f"Not a valid template file (bad JSON): {exc}") from exc
    # Tolerate a bare list of templates as well as the envelope -- that is
    # what someone hand-assembling a file is most likely to produce.
    if isinstance(data, list):
        templates = data
    elif isinstance(data, dict):
        if data.get("format") not in (None, EXPORT_FORMAT):
            raise ValueError(f"Unrecognised file format: {data.get('format')!r}")
        templates = data.get("templates")
        if templates is None:
            raise ValueError("File contains no 'templates' section")
    else:
        raise ValueError("Not a valid template file")
    if not isinstance(templates, list):
        raise ValueError("'templates' must be a list")
    out = []
    for t in templates:
        if not isinstance(t, dict):
            raise ValueError("Each template must be an object")
        name = str(t.get("name") or "").strip()
        if not name:
            raise ValueError("A template in the file has no name")
        out.append({**t, "name": name})
    return out


def import_templates(payload: bytes | str, *, overwrite: bool = False) -> dict:
    """Add the file's templates to this install.

    Returns {"added": [...], "replaced": [...], "skipped": [...]}. A name
    that already exists is left alone unless `overwrite` is set, so a
    shared file can never silently clobber a locally tuned template.
    Validation of every template happens before anything is written, so a
    file with one bad entry imports nothing rather than half of itself.
    """
    incoming = _parse_export(payload)
    cleaned: list[tuple[str, dict]] = []
    for t in incoming:
        try:
            cleaned.append((t["name"], _clean_fields(t)))
        except ValueError as exc:
            raise ValueError(f"Template {t['name']!r}: {exc}") from exc

    existing = {t["name"] for t in list_ea_templates()}
    result: dict[str, list[str]] = {"added": [], "replaced": [], "skipped": []}
    for name, clean in cleaned:
        if name in existing:
            if not overwrite:
                result["skipped"].append(name)
                continue
            result["replaced"].append(name)
        else:
            result["added"].append(name)
        save_ea_template(name, clean)
    log.info("[EATemplates] import: %d added, %d replaced, %d skipped",
             len(result["added"]), len(result["replaced"]), len(result["skipped"]))
    return result
