/**
 * Every setting on the Risk tab, as data.
 *
 * Transcribed from `frontend/pages/settings/_risk.py` by parsing its source.
 * **Each `key` is a column of `vantage_risk_settings`**, and
 * `tests/api/test_settings_writes_reach_the_store.py` checks that against the
 * schema rather than trusting this comment.
 *
 * That check exists because the first React version of this tab offered four
 * fields, two of which were not columns at all: `risk_pct` (a column on a
 * different table) and `daily_loss_limit_pct` (a column nowhere). Both raised
 * on save, so the operator typed a number, saw it stay in the box, and it was
 * never stored. The real names are `risk_per_trade_pct` and
 * `max_daily_loss_pct`.
 *
 * The wording matters as much as the names. These decide how much a single
 * trade can lose and when the account stops for the day; a paraphrase is how
 * somebody sets the wrong one.
 */

export interface RiskField {
  key: string;
  label: string;
  hint?: string;
  kind: "number" | "toggle" | "choice";
  choices?: { value: string; label: string }[];
  /** The switch this one does nothing without, and where that switch lives. */
  dependsOn?: { key: string; label: string };
}

export interface RiskGroup {
  title: string;
  blurb?: string;
  fields: RiskField[];
}

export const RISK_GROUPS: RiskGroup[] = [
  {
    title: "Per trade",
    blurb: "How much one entry may risk, and how many may be open at once.",
    fields: [
      {
        key: "risk_per_trade_pct", label: "Risk per trade (%)", kind: "number",
        hint: "Percentage of the account risked on each entry. The service clamps this.",
      },
      {
        key: "max_risk_per_trade_pct", label: "Maximum risk per trade (%)",
        kind: "number",
        hint: "A ceiling on the value above, however it was arrived at.",
      },
      {
        key: "max_open_trades", label: "Maximum open trades", kind: "number",
        hint: "New entries are refused once this many positions are open. A resting order holds a slot from placement until the position it becomes is closed.",
      },
      {
        key: "max_lot_size", label: "Maximum lot size", kind: "number",
        hint: "A hard ceiling applied after sizing, whatever the risk maths asks for.",
      },
    ],
  },
  {
    title: "Stopping for the day",
    blurb:
      "Both of the first two measure from the day's OPENING balance. The give-back guard is the one that can see a day which goes well and then does not.",
    fields: [
      {
        key: "max_daily_loss_pct", label: "Max daily loss (%)", kind: "number",
        hint: "Measured from the opening balance.",
      },
      {
        key: "max_total_drawdown_pct", label: "Max total drawdown (%)",
        kind: "number", hint: "Measured from the account's peak.",
      },
      {
        key: "giveback_guard_enabled", kind: "toggle",
        label: "Stop for the day after giving back today's profit",
        hint: "The limits above measure from the open, so neither can see a day that goes +$349 and closes -$88 — which is what 2026-08-17 did.",
      },
      {
        key: "giveback_arm_usd", label: "Arm above profit ($)", kind: "number",
        hint: "The guard does nothing until the day has been at least this far ahead. Ordinary churn around break-even must not be able to end the day.",
        dependsOn: { key: "giveback_guard_enabled", label: "the give-back guard" },
      },
      {
        key: "giveback_pct", label: "Give-back limit (%)", kind: "number",
        hint: "How much of the day's peak profit may be handed back before trading stops.",
        dependsOn: { key: "giveback_guard_enabled", label: "the give-back guard" },
      },
    ],
  },
  {
    title: "Circuit breaker",
    blurb: "A cooling-off period after a run of losing trades.",
    fields: [
      { key: "circuit_breaker_enabled", label: "Enable circuit breaker", kind: "toggle" },
      {
        key: "circuit_breaker_losses", label: "Consecutive losses to trigger",
        kind: "number",
        dependsOn: { key: "circuit_breaker_enabled", label: "the circuit breaker" },
      },
      {
        key: "circuit_breaker_cooldown_mins", label: "Cooldown period (minutes)",
        kind: "number",
        dependsOn: { key: "circuit_breaker_enabled", label: "the circuit breaker" },
      },
    ],
  },
  {
    title: "Which signals are taken",
    fields: [
      {
        key: "exclude_high_risk", label: "Exclude high-risk signals", kind: "toggle",
        hint: "Skip signals the parser marked high-risk.",
      },
      {
        key: "htf_bias_gate_enabled", label: "Only trade with the trend",
        kind: "toggle",
        hint: "Refuse entries against the higher-timeframe bias.",
      },
      {
        key: "min_fill_delay_enabled", label: "Ignore signals that fill immediately",
        kind: "toggle",
        hint: "A signal whose price is already at entry has usually already moved.",
      },
      {
        key: "min_fill_delay_s", label: "Minimum seconds before a fill counts",
        kind: "number",
        dependsOn: { key: "min_fill_delay_enabled", label: "the immediate-fill filter" },
      },
      {
        key: "resting_revalidation_enabled",
        label: "Re-check resting orders before they fill", kind: "toggle",
        hint: "A limit placed an hour ago is a view of an hour-old market.",
      },
      {
        key: "hour_blocklist_enabled",
        label: "Skip live execution during measured toxic hours", kind: "toggle",
        hint: "Hours this account has measurably lost money in.",
      },
      {
        key: "stale_better_fill_cap_enabled",
        label: "Cap how far past its zone a queued signal may fill",
        kind: "toggle",
        hint: "A fill far beyond the zone on the good side is a different setup, not a better price.",
      },
      {
        key: "stale_better_fill_cap_pts", label: "Better-fill cap (points)",
        kind: "number",
        dependsOn: { key: "stale_better_fill_cap_enabled", label: "the better-fill cap" },
      },
      {
        key: "pending_momentum_gate_enabled",
        label: "Check M5 momentum before releasing a queued signal",
        kind: "toggle",
        hint: "Defer a signal the last candle disagrees with. Needs no Dynamic Profit Management.",
      },
    ],
  },
  {
    title: "Exposure",
    blurb: "What may be open in opposite directions at the same time.",
    fields: [
      {
        key: "burst_hedge_guard_enabled",
        label: "One direction per queued-signal release", kind: "toggle",
        hint: "After a pause, the backlog empties in one pass. This stops it opening both sides.",
      },
      {
        key: "internal_hedge_mode", label: "Hedging", kind: "choice",
        choices: [
          { value: "off", label: "Off — no restriction (default)" },
          { value: "self_hedge", label: "Self-Hedge Guard — block opposing positions" },
          { value: "net_exposure", label: "Net Exposure Cap — limit net directional lots" },
        ],
      },
      {
        key: "internal_net_exposure_max_lots", label: "Net exposure cap (lots)",
        kind: "number",
        dependsOn: { key: "internal_hedge_mode", label: "the Net Exposure Cap mode" },
      },
    ],
  },
  {
    title: "Managing an open trade",
    fields: [
      {
        key: "dpm_enabled", label: "Hand off to adaptive management", kind: "toggle",
        hint: "Dynamic Position Management takes over SL and partials once a trade is open.",
      },
      {
        key: "profit_close_usd", label: "Close at profit ($)", kind: "number",
        hint: "Close any open trade once it is this far ahead in dollars. 0 is off. This runs on every open trade whether or not adaptive management is on — it sits here for grouping, not because it needs it.",
      },
    ],
  },
  {
    title: "Risk governor",
    blurb:
      "The Tier-1 safety layer. It also takes over position sizing and adds its own pre-trade gates, which is why the give-back guard above is deliberately NOT behind it.",
    fields: [
      { key: "risk_governor_enabled", label: "Risk governor", kind: "toggle" },
    ],
  },
];

/** Every key the tab can write, for the tests that count them. */
export const RISK_KEYS = RISK_GROUPS.flatMap((g) => g.fields.map((f) => f.key));
