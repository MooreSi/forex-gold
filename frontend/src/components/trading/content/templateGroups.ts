/**
 * How the EA template's ~100 fields are laid out on screen.
 *
 * The fields themselves — name, type, default, allowed values — come from the
 * backend (`GET /api/trading/templates/schema`), because a hundred field types
 * maintained in two languages drift the first time one is added to only one of
 * them. What lives here is the part the backend has no opinion about: which
 * fields belong together, in what order, and what to call them in English.
 *
 * **Nothing is unreachable.** A field this file does not mention falls into
 * "Other settings" rather than disappearing — that is the rule that keeps a
 * newly added backend field editable without a matching change here.
 */
export interface FieldGroup {
  id: string;
  title: string;
  /** One line on what this group decides. Shown under the title. */
  blurb: string;
  icon: string;
  /** Exact field names, in the order they should appear. */
  fields?: string[];
  /** Or a rule, for the repetitive ladders. */
  match?: (name: string) => boolean;
}

export const OTHER_GROUP_ID = "other";

export const FIELD_GROUPS: FieldGroup[] = [
  {
    id: "entry",
    title: "Entry and lots",
    blurb: "How many orders go out, at what size, and what blocks them.",
    icon: "positions",
    fields: [
      "mode", "anchor", "anchors", "pendings", "lot_anchor", "lot_pending",
      "pending_mode", "grid_legs", "grid_step_pts", "gold_half_pip_anchor",
      "anc_shave", "cancel_pending", "cancel_pending_level",
      "late_guard_pips", "signal_max_age_sec", "max_spread_pips", "slippage",
      "sig_guard", "sig_guard_pips", "tg_cmd_enabled",
    ],
  },
  {
    id: "stops",
    title: "Stop loss",
    blurb: "Where the stop goes when the signal does not carry a usable one.",
    icon: "risk",
    fields: [
      "sl_pips", "auto_sl", "risk_pct", "tpsl_mode", "signal_rr_ratio",
      "safety_cap_pips", "guard_pips", "manual_sl_push_pips",
      "use_emergency_sl", "emergency_sl_mult",
    ],
  },
  {
    id: "ladder",
    title: "Take-profit ladder — anchor legs",
    blurb: "Each level's distance and the share of the position closed there.",
    icon: "ladder",
    match: (n) => /^tp(10|[1-9])_(pips|pct)$/.test(n),
  },
  {
    id: "ladder-rules",
    title: "Ladder behaviour",
    blurb: "Whether the signal's own targets win, and what happens at the last one.",
    icon: "target",
    fields: [
      "tp_from_telegram", "partials", "close_full_on_last",
      "group_tp_action", "tp1_trigger_level",
    ],
  },
  {
    id: "ladder-pending",
    title: "Take-profit ladder — pending legs",
    blurb: "The same ladder for resting entries, which often wants different targets.",
    icon: "ladder",
    match: (n) => /^tp_pen(10|[1-9])_(pips|pct)$/.test(n) || n === "tp_pen_from_telegram",
  },
  {
    id: "breakeven",
    title: "Breakeven",
    blurb: "When the stop moves to entry, and how far past it.",
    icon: "clock",
    fields: ["be_mode", "be_buffer_pts", "be_trigger"],
  },
  {
    id: "trailing",
    title: "Trailing stop",
    blurb: "How the stop follows price once the trade is running.",
    icon: "activity",
    fields: [
      "trail_mode", "trail_activation", "trail_distance", "trail_step",
      "trail_padding",
    ],
  },
  {
    id: "staged",
    title: "Staged stop ratchet",
    blurb: "Up to three fixed steps that pull the stop forward as price moves.",
    icon: "ladder",
    match: (n) => n.startsWith("sl_stage"),
  },
  {
    id: "atr",
    title: "Volatility (ATR)",
    blurb: "Sizes the stop and the first target from measured volatility instead of fixed pips.",
    icon: "percent",
    fields: [
      "use_dynamic_atr", "atr_period", "atr_sl_mult", "atr_tp1_mult",
      "atr_ladder_scale",
    ],
  },
  {
    id: "protect",
    title: "Protection and harvesting",
    blurb: "Closes the whole group on a combined loss or a combined profit.",
    icon: "warning",
    fields: [
      "equity_protect", "basket_harvest_threshold", "harvest_enabled",
      "harvest_threshold", "harvest_pips",
    ],
  },
];

/** Human wording for the fields whose names do not say enough on their own. */
export const FIELD_LABELS: Record<string, string> = {
  mode: "Mode",
  anchor: "Anchor placement",
  anchors: "Anchor legs",
  pendings: "Pending legs",
  lot_anchor: "Lots per anchor leg",
  lot_pending: "Lots per pending leg",
  pending_mode: "Pending placement",
  grid_legs: "Grid legs (legacy)",
  grid_step_pts: "Grid step",
  gold_half_pip_anchor: "Half-pip anchor (gold)",
  anc_shave: "Shave the anchor into the zone",
  cancel_pending: "Cancel pendings on a TP",
  cancel_pending_level: "Cancel pendings at TP level",
  late_guard_pips: "Reject a late signal beyond",
  signal_max_age_sec: "Reject a signal older than",
  max_spread_pips: "Refuse above spread",
  slippage: "Allowed slippage",
  sig_guard: "Block a second trade on this channel",
  sig_guard_pips: "…only within",
  tg_cmd_enabled: "Accept Telegram commands",
  sl_pips: "Stop distance",
  auto_sl: "Set a stop when the signal has none",
  risk_pct: "Risk per trade",
  tpsl_mode: "SL/TP at the broker",
  signal_rr_ratio: "Target R:R when deriving a TP",
  safety_cap_pips: "Never risk more than",
  guard_pips: "Guard distance",
  manual_sl_push_pips: "Manual stop nudge",
  use_emergency_sl: "Emergency stop",
  emergency_sl_mult: "Emergency stop multiple",
  tp_from_telegram: "Use the signal's own targets",
  tp_pen_from_telegram: "Pendings use the signal's targets",
  partials: "Close in parts",
  close_full_on_last: "Close everything at the last target",
  group_tp_action: "Apply a TP to the whole group",
  tp1_trigger_level: "Treat this level as TP1",
  be_mode: "Breakeven point",
  be_buffer_pts: "Breakeven buffer",
  be_trigger: "Move to breakeven at TP",
  trail_mode: "Trail type",
  trail_activation: "Start trailing after",
  trail_distance: "Trail distance",
  trail_step: "Trail step",
  trail_padding: "Trail padding",
  use_dynamic_atr: "Size from ATR",
  atr_period: "ATR period",
  atr_sl_mult: "ATR stop multiple",
  atr_tp1_mult: "ATR first-target multiple",
  atr_ladder_scale: "Scale the whole ladder by ATR",
  equity_protect: "Close the group at a floating loss of",
  basket_harvest_threshold: "Close the group at a combined profit of",
  harvest_enabled: "Harvest on aggregate profit",
  harvest_threshold: "Harvest threshold",
  harvest_pips: "Harvest distance",
};

/** The unit shown after a field's input. Nothing here is decoration: "50"
 *  means very different things in pips, points, percent and dollars. */
export const FIELD_UNITS: Record<string, string> = {
  grid_step_pts: "pts",
  be_buffer_pts: "pts",
  lot_anchor: "lots",
  lot_pending: "lots",
  sl_pips: "pips",
  risk_pct: "%",
  equity_protect: "$",
  basket_harvest_threshold: "$",
  harvest_threshold: "$",
  harvest_pips: "pips",
  late_guard_pips: "pips",
  sig_guard_pips: "pips",
  trail_distance: "pips",
  trail_step: "pips",
  trail_activation: "pips",
  trail_padding: "pips",
  max_spread_pips: "pips",
  safety_cap_pips: "pips",
  guard_pips: "pips",
  manual_sl_push_pips: "pips",
  signal_max_age_sec: "s",
  slippage: "pts",
};

/** Fields where 0 is not "none" but a real setting, or vice versa. */
export const FIELD_HINTS: Record<string, string> = {
  risk_pct: "0 sizes from the lot fields instead. The EA template override on Trading > Risk replaces both.",
  equity_protect: "0 is off.",
  basket_harvest_threshold: "0 is off.",
  late_guard_pips: "0 is no guard.",
  sig_guard_pips: "0 blocks any trade on this channel in the same direction.",
  cancel_pending_level: "0 never cancels.",
  tp1_trigger_level: "Which ladder level counts as TP1 for breakeven and cancels.",
  harvest_enabled:
    "Closes on the group's combined profit, so this template cannot be backtested.",
  mode: "Grid templates place several resting legs and cannot be backtested.",
};

export function labelFor(name: string): string {
  if (FIELD_LABELS[name]) return FIELD_LABELS[name];
  // A ladder field, or one added to the backend since this file was written.
  return name.replace(/_/g, " ").replace(/\btp pen\b/, "pending TP");
}
