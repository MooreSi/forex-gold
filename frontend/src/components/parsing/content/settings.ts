/**
 * Every switch on the Parsing tab, as data.
 *
 * Transcribed from the NiceGUI page by parsing its source. The wording matters:
 * each of these decides whether a Telegram signal is traded, and a description
 * that drifts from what the backend does is how somebody turns the wrong one on.
 *
 * **This list is why the tab has a test that counts rows.** The Parsing tab
 * shipped from the 2026-08-25 upstream merge with its settings section an empty
 * stub and its whole body parked in a function nothing called: every switch
 * disappeared from the UI while staying fully wired in the backend, so
 * `immediate_market_entry` could not be turned on and a bare "Buy Now" signal
 * was missed. Nothing went red, because the render test pinned the auth wizard
 * and not the settings. `ParsingPanel.test.tsx` now asserts every row here
 * reaches the screen.
 */

export interface ParsingToggle {
  key: string;
  label: string;
  description: string;
  /** What an install that never touched this setting does. */
  defaultOn: boolean;
}

export interface ParsingCategory {
  badge: string;
  /** A semantic token name, not a colour: see index.css. */
  tone: "accent" | "remote" | "profit" | "warning";
  toggles: ParsingToggle[];
}

export const PARSING_CATEGORIES: ParsingCategory[] = [
  {
    badge: "PARSING",
    tone: "accent",
    toggles: [
      {
        key: "lk_enable_tp_parsing",
        label: "Enable TP Parsing",
        description: "Use this signal's own stated TP levels when a new entry parses. When OFF, TP levels are stripped before execution.",
        defaultOn: true,
      },
      {
        key: "lk_enable_sl_parsing",
        label: "Enable SL Parsing",
        description: "Use this signal's own stated Stop Loss when a new entry parses. When OFF, it is replaced by the channel template's SL Pips, or the Fallback SL Distance below. Applies to new entries only \u2014 the RISK FREE / BE and CLOSE ALL triggers are separate.",
        defaultOn: true,
      },
      {
        key: "lk_enable_second_message_tp_sl",
        label: "TP/SL in Second Message",
        description: "Hold a signal that arrives with a direction and entry but no levels, and complete it from a follow-up message sent within the match window below. Executes bare if nothing arrives in time.",
        defaultOn: false,
      },
    ],
  },
  {
    badge: "EXECUTION",
    tone: "remote",
    toggles: [
      {
        key: "auto_execute_signals",
        label: "Auto-Execution",
        description: "Incoming Telegram signals are automatically traded when ON. Manual signals always require explicit execution. Ensure Algo Trading is enabled in the MT5 Terminal \u2014 MT5 disables it automatically after restarts or account switches.",
        defaultOn: false,
      },
      {
        key: "lk_enable_close_all_parsing",
        label: "Enable CLOSE ALL Parsing",
        description: "Automatically close the triggering channel's own open trade when it sends a CLOSE ALL trigger phrase.",
        defaultOn: true,
      },
      {
        key: "lk_enable_risk_free_be_parsing",
        label: "Enable RISK FREE / BE Parsing",
        description: "Move SL to entry price (breakeven) when the channel sends a breakeven/risk-free trigger phrase.",
        defaultOn: true,
      },
      {
        key: "immediate_market_entry",
        label: "Immediate Market Buy/Sell",
        description: "Reads all Telegram channels for bare 'Buy Now'/'Sell Now' messages and enters at current market price immediately. When the follow-up signal with SL and TP levels arrives the open trade is updated automatically. Applies to all strategies.",
        defaultOn: false,
      },
      {
        key: "lk_entry_realignment",
        label: "Entry Realignment",
        description: "Limit Runner only. If the market has already moved through the signalled zone by the time the order would be placed, enters at current market price instead and shifts SL/TP by the same distance \u2014 otherwise the broker rejects a now-invalid limit price and the trade is lost entirely.",
        defaultOn: false,
      },
      {
        key: "lk_enable_mirror_copy",
        label: "Reverse / Mirror Copy",
        description: "Invert BUY\u2194SELL and mirror Stop Loss and every TP through the entry zone, so the trade placed is the exact opposite of the one signalled. Applies to every channel.",
        defaultOn: false,
      },
    ],
  },
  {
    badge: "SAFETY",
    tone: "profit",
    toggles: [
      {
        key: "lk_ignore_media_messages",
        label: "Ignore Media Messages",
        description: "Ignore messages containing photos, videos, or documents (only parse plain text).",
        defaultOn: true,
      },
      {
        key: "lk_ignore_forwarded_messages",
        label: "Ignore Forwarded Messages",
        description: "Do not execute trades from messages forwarded from other channels.",
        defaultOn: false,
      },
    ],
  },
  {
    badge: "RESEARCH",
    tone: "remote",
    toggles: [
      {
        key: "tg_decision_log_enabled",
        label: "Signal Decision Log",
        description:
          "Records what the app decided about every Telegram signal — executed or blocked, on both the normal and the Immediate Market Buy/Sell path — what the trade then did, and what four gates that are currently switched OFF would have decided. It changes no trading decision and blocks nothing: it only writes down what happened, so a gate can be judged on this account's own trades before being switched on. Costs nothing on the order path; nothing is fetched.",
        defaultOn: false,
      },
      {
        key: "tg_contradiction_log_enabled",
        label: "Signal Contradiction Study",
        description:
          "Records what would happen when two sources disagree about direction — a Telegram channel against an internal engine, or one channel against another. Each policy (first wins, freshest wins, half size) has its verdict written down; none of them is applied, and no trade is blocked, resized or cancelled. Switching it on also puts Telegram signals on the shared signal bus, which is what makes the disagreement visible at all; with it off, the bus is exactly as it was.",
        defaultOn: false,
      },
    ],
  },
  {
    badge: "MARKET GUARD",
    tone: "warning",
    toggles: [
      {
        key: "lk_queue_closed_market_limits",
        label: "Queue Closed Market Limits",
        description: "Hold BUY/SELL LIMIT signals that arrive while the market is shut for the weekend, then place them automatically when it reopens. Without this they are dropped and the setup is lost.",
        defaultOn: false,
      },
    ],
  },
];

/** Every switch key, flattened. The tab's own completeness check. */
export const PARSING_KEYS: string[] = PARSING_CATEGORIES.flatMap((c) =>
  c.toggles.map((t) => t.key),
);
