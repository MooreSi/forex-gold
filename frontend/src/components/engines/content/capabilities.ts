/**
 * The Reversal engine's capability switches, as data.
 *
 * Transcribed from `frontend/pages/reversal_panel/_capabilities.py`. One of
 * them does nothing on its own, and saying so is not optional: a switch that
 * silently depends on another is one the owner turns on, watches do nothing,
 * and concludes is broken. `dependsOn` is rendered, not just commented.
 */
export interface Capability {
  key: string;
  label: string;
  description: string;
  /** The setting that must also be on, and where it lives. */
  dependsOn?: { key: string; label: string; where: string };
}

export const CAPABILITIES: Capability[] = [
  {
    key: "meta_label_gate_enabled",
    label: "Ask the meta-labeller",
    description:
      "Score each candidate signal with the trained model and skip the ones it rates below the threshold.",
  },
  {
    key: "session_liquidity_gate_enabled",
    label: "Session liquidity gate",
    description:
      "Refuse entries in the thinnest part of the session, where the spread costs more than the edge.",
  },
  {
    key: "htf_bias_asian_exempt",
    label: "…but not in the Asian session: ignore the trend there",
    description:
      "Stands the higher-timeframe trend gate down for 00–07 UTC and changes nothing else, anywhere.",
    dependsOn: {
      key: "htf_bias_gate_enabled",
      label: "Only trade with the trend",
      where: "Settings → Risk",
    },
  },
  {
    key: "event_tier_gate_enabled",
    label: "Event tier gate",
    description: "Hold entries around the highest-impact scheduled releases.",
  },
  {
    key: "vol_target_sizing_enabled",
    label: "Volatility-target sizing",
    description:
      "Scale the position with measured volatility instead of using a flat risk percentage.",
  },
];
