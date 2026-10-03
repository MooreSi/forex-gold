/**
 * What each EA template setting does, shown when you hover over it.
 *
 * Asked for on 2026-10-01: "add tool tips when you hover over each box to
 * provide details as to what each setting does". Each line here was written
 * from the code that reads the field (ea_templates.py's DEFAULTS, the EA's
 * ManageTemplate, signals/resolution.py's template filters), not from the
 * label, so where a field does less than its name suggests the text says so.
 *
 * A field missing from this map still gets a tooltip: `helpFor` falls back to
 * naming the raw setting, so a field added to the backend is never silent.
 */
import { LADDER_FIELD, labelFor } from "./templateGroups";

/** Read by nothing on 2026-10-01: neither the app nor the EA acts on these.
 *  Said in the tooltip so nobody tunes a box that changes nothing. */
export const UNUSED_FIELDS = new Set(["auto_sl", "signal_max_age_sec", "anc_shave"]);

const UNUSED_NOTE = " Not acted on by the app or the EA yet: changing it has no effect.";

export const FIELD_HELP: Record<string, string> = {
  mode:
    "Single opens the anchor leg(s) only. Grid also stages resting pending legs "
    + "around the entry, each with its own lot and TP ladder.",
  anchor:
    "Where a grid's TPs and breakeven are measured from. Unified: every leg shares "
    + "the price the grid was staged from, so legs filled deeper earn more to the same "
    + "target. Distributed: each leg measures from its own fill.",
  anchors: "How many legs open at or near market when the signal fires.",
  pendings:
    "How many resting limit legs are placed in grid mode, in addition to the anchors.",
  lot_anchor: "Lot size of each anchor leg.",
  lot_pending: "Lot size of each pending leg.",
  pending_mode:
    "Zone: pending legs span the signal's own entry zone (a leg landing on the wrong "
    + "side of price is skipped). Step: legs are placed every grid step away from the "
    + "anchor's price, so none are lost.",
  grid_legs:
    "Replaced by Pending legs. The EA only reads this when Pending legs is missing, "
    + "which never happens for a template saved here.",
  grid_step_pts: "Distance between pending legs when Pending placement is Step, in points.",
  gold_half_pip_anchor: "Round grid leg prices to the half pip, for gold feeds that quote that finely.",
  anc_shave: "Trim the anchor's entry back toward the zone rather than chasing price.",
  cancel_pending:
    "Older switch: cancel the grid's still-resting legs as soon as one leg fills. "
    + "Cancel pendings at TP level is the finer version.",
  cancel_pending_level:
    "When a leg clears this TP level, the grid's still-resting legs are cancelled. 0 never cancels.",
  late_guard_pips:
    "Grid templates only: skip the signal if price is already more than this far "
    + "beyond the entry zone. The signal stays active in case price comes back. 0 is no guard.",
  signal_max_age_sec: "Meant to reject a Telegram signal older than this many seconds at fill time.",
  max_spread_pips:
    "Skip the signal while the spread is wider than this. It stays active for when "
    + "the spread narrows. 0 is no limit.",
  slippage: "How far the fill may move from the requested price, in points, before the broker rejects it.",
  sig_guard:
    "Block a new trade from this channel while one in the same direction is already open.",
  sig_guard_pips:
    "Narrows the block above to an open trade whose entry is within this many pips. "
    + "0 blocks any same-direction trade on the channel.",
  tg_cmd_enabled:
    "Allow Telegram bot commands (close, move stop and so on) on trades this template opened.",

  sl_pips: "Stop distance from entry, used when the signal has no usable stop of its own.",
  auto_sl: "Meant to push the signal's stop to the broker rather than tracking it internally.",
  risk_pct:
    "Size each trade to risk this % of the account at the stop. 0 uses the lot fields "
    + "instead. The EA template override on Trading > Risk replaces both.",
  tpsl_mode:
    "On: the stop and final TP sit at the broker. Stealth: the TP is tracked by the EA "
    + "and never shown to the broker. Off: no broker TP.",
  signal_rr_ratio:
    "Skip a signal whose own TP1 is closer than this many times its stop "
    + "(e.g. 1.0 needs TP1 at least as far as the SL). 0 is no filter.",
  safety_cap_pips: "A floor the EA never tightens a stop inside of, measured from price.",
  guard_pips:
    "Minimum gap kept between price and any stop the EA places or moves, so a "
    + "breakeven move is not rejected as an invalid stop.",
  manual_sl_push_pips: "How far a manual 'push stop' command from Telegram or the panel moves the stop.",
  use_emergency_sl:
    "If the stop is removed or pushed wider, force it back to the emergency multiple "
    + "of the trade's original stop distance.",
  emergency_sl_mult: "The emergency stop's distance, as a multiple of the original stop distance.",

  tp_from_telegram:
    "Anchor legs take their TP prices from the Telegram message instead of the pips "
    + "below. Take % still applies. Engines have no message, so they always use the pips.",
  tp_pen_from_telegram:
    "The same for pending legs: TP prices from the Telegram message, Take % from below.",
  partials:
    "Close part of the position at each level. Off: nothing closes until the last "
    + "configured level, which closes everything.",
  close_full_on_last:
    "The last configured level closes whatever is still open, whatever its own Take %. "
    + "Off leaves the remainder running on the trail or stop.",
  group_tp_action:
    "Grid only: the first TP any leg clears cancels the remaining resting legs and "
    + "moves every other open leg's stop to breakeven.",
  tp1_trigger_level:
    "Clearing this TP level also starts the trail, whichever comes first with "
    + "Start trailing after.",

  be_mode: "Entry: breakeven is the entry price. Entry + buffer: a few points past it, to cover costs.",
  be_buffer_pts: "How far past entry the breakeven stop goes when Breakeven point is Entry + buffer, in points.",
  be_trigger: "The TP level at which the stop moves to breakeven.",

  trail_mode:
    "Off: no trail. Step: follow price at Trail distance, moving in Trail step jumps. "
    + "Candle: behind recent candle lows/highs. Fractal: behind the last confirmed "
    + "fractal. TP: to each cleared TP level. Staged: the ratchet rungs below.",
  trail_activation:
    "Profit, in pips, before the trail starts moving the stop. 0 trails from the start.",
  trail_distance: "How far behind price the trailing stop sits, in pips.",
  trail_step: "The minimum improvement, in pips, before the trailing stop is moved again. 0 moves on any improvement.",
  trail_padding: "Extra pips added to Trail distance, so the step trail sits that much further behind price.",

  use_dynamic_atr: "Size the stop and TP1 from ATR (measured volatility) instead of fixed pips.",
  atr_period: "How many candles the ATR is averaged over.",
  atr_sl_mult: "Stop distance = ATR x this.",
  atr_tp1_mult: "TP1 distance = ATR x this.",
  atr_ladder_scale:
    "Rescale the whole anchor ladder so TP1 lands on its ATR multiple, keeping the "
    + "levels' spacing. Off: only the stop and TP1 follow ATR.",

  equity_protect:
    "Close every trade in this template's group once their combined floating loss "
    + "reaches this many dollars. 0 is off.",
  basket_harvest_threshold:
    "Close every trade in the group once their combined floating profit reaches "
    + "this many dollars. 0 is off.",
  harvest_enabled: "Close a trade once its own floating profit reaches the harvest threshold or distance.",
  harvest_threshold: "Profit in dollars at which a single trade is harvested.",
  harvest_pips:
    "Profit in pips at which a single trade is harvested. Any value above 0 wins over "
    + "the dollar threshold. 0 is off.",
};

const STAGE_RE = /^sl_stage(\d+)_(trigger_pips|target_pips|remove_tp)$/;

/** The tooltip for one field. Never empty. */
export function helpFor(name: string): string {
  const ladder = LADDER_FIELD.exec(name);
  if (ladder) {
    const [, prefix, n, kind] = ladder;
    const legs = prefix === "tp" ? "anchor" : "pending";
    return kind === "pips"
      ? `TP${n} for ${legs} legs: how far from entry, in pips, this level sits. 0 leaves the level unused.`
      : `Take % at TP${n} for ${legs} legs: the share of the original position closed when price `
        + "reaches this level. A level can only close what earlier levels left open.";
  }
  const stage = STAGE_RE.exec(name);
  if (stage) {
    const [, n, kind] = stage;
    if (kind === "trigger_pips") {
      return `Rung ${n} fires once profit reaches this many pips. 0 leaves the rung unused.`;
    }
    if (kind === "target_pips") {
      return `Where rung ${n} moves the stop, in pips from entry: negative still risks a `
        + "smaller loss, 0 is breakeven, positive locks in profit.";
    }
    return `When rung ${n} fires, also remove the take-profit so the trade rides the trail.`;
  }
  const base = FIELD_HELP[name] ?? `The template's "${labelFor(name)}" setting (${name}).`;
  return UNUSED_FIELDS.has(name) ? base + UNUSED_NOTE : base;
}
