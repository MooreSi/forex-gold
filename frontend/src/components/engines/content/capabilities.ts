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
  {
    key: "re_cme_context_enabled",
    label: "Read CME futures context (not connected yet)",
    // The wording is the point, not decoration. The failure it guards against
    // is the owner turning this on, seeing no change, and concluding the
    // engine is broken — or worse, believing a later decision was informed by
    // data this build does not have. `test_cme_context_switch.py` reads the
    // text below and fails if it stops admitting that, so do not soften it.
    //
    // That test takes the FIRST match for the key in this file and reads
    // forward, so nothing above may repeat the phrases it looks for: a comment
    // that quotes them satisfies the assertion on its own and the real
    // description could then say anything at all.
    description:
      "This broker quotes spot gold bid/ask with no Last, so there is no trade " +
      "side and every 'volume' in this app is tick volume — a count of quote " +
      "changes, not size. GC futures are the lit venue where gold prints real " +
      "size, and the only route to measured flow instead of a proxy. There is " +
      "NO CME FEED in this build: no entitlement, no client, no ingest. " +
      "Turning this on records the intent and CHANGES NOTHING the engine " +
      "decides. The data it would use — daily GC volume and open interest — " +
      "is published free by CME; only real-time streaming is a paid " +
      "entitlement, and this engine does not need it. Whether it is worth " +
      "building at all is still the owner's call: nothing has yet measured " +
      "that futures flow predicts anything about these trades. See " +
      "docs/simon-handover/039.",
  },
];
