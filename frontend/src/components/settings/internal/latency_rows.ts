/**
 * Settings > Latency: the shapes the backend sends, and how they become rows.
 *
 * Every hop, probe and broker figure is turned into the same row so one table
 * draws all of them, in the order a signal travels.
 */

export interface HopStats { n?: number; p50?: number; p90?: number; max?: number }
export interface Hop {
  id: string; label: string; detail: string; amber_ms: number; stats: HopStats; slow: boolean;
}
export interface RecentRow { key: string; label: string; at: number; hops: Record<string, number | null> }
export interface PipelineView { hops: Hop[]; recent: RecentRow[] }
export interface Wait { id: string; label: string; seconds: number; detail: string }
export interface Probe {
  ok: boolean; ms: number | null; detail: string; amber_ms: number;
  session_dc?: number | null; nearest_dc?: number | null;
}
export interface BrokerServer {
  n: number; median_ms: number; p90_ms: number; max_ms: number; over_5s: number; slow?: boolean;
}
export interface Broker { available: boolean; servers: Record<string, BrokerServer>; days?: number }
export interface LocalReport {
  probes?: Record<string, Probe>;
  pipelines?: Record<string, PipelineView>;
  structural?: Wait[];
  broker?: Broker;
  broker_amber_ms?: number;
  error?: string;
}
export interface VpsReport {
  ok: boolean; rtt_ms: number | null; remote: LocalReport | null; detail: string; amber_ms: number;
}
export interface Passive { pipelines: Record<string, PipelineView>; structural: Wait[]; paired: boolean }
export interface CheckResult { local: LocalReport; vps: VpsReport | null }

export type RowState = "ok" | "slow" | "fail" | "none";

export interface Row {
  key: string;
  label: string;
  detail: string;
  p50: number | null;
  p90: number | null;
  max: number | null;
  n: number | null;
  state: RowState;
  note: string;
}

/** "—" for nothing, never "0 ms": an unmeasured hop is not a fast one. */
export function fmtMs(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v < 1) return "<1 ms";
  if (v < 1000) return `${Math.round(v)} ms`;
  return `${(v / 1000).toFixed(v < 10_000 ? 2 : 1)} s`;
}

export function hopRows(view: PipelineView | undefined, prefix = ""): Row[] {
  return (view?.hops ?? []).map((h) => {
    const measured = h.stats && h.stats.n != null;
    return {
      key: `${prefix}${h.id}`,
      label: `${prefix}${h.label}`,
      detail: h.detail,
      p50: measured ? h.stats.p50 ?? null : null,
      p90: measured ? h.stats.p90 ?? null : null,
      max: measured ? h.stats.max ?? null : null,
      n: measured ? h.stats.n ?? null : null,
      state: !measured ? "none" : h.slow ? "slow" : "ok",
      note: measured ? "" : "no signal has crossed this hop since the app started",
    };
  });
}

const PROBE_LABELS: Record<string, [string, string]> = {
  telegram: ["Telegram API round trip", "One light request to Telegram's data centre and back."],
  loop: ["Event loop lag", "How late a 50 ms timer fires: time this app spent busy elsewhere."],
  db: ["Database worker round trip", "The scanner makes ~200 of these a cycle."],
  ea: ["EA link round trip", "Ping to pong over the local socket: TCP plus the EA's 200 ms timer, or longer while it is busy managing trades."],
  bridge: ["MT5 bridge round trip", "The bridge's health endpoint (Mac/Wine) or the in-process call (Windows)."],
  tick: ["Fresh tick from MT5", "The price read taken just before an order."],
};

export function probeRows(probes: Record<string, Probe> | undefined, keys: string[], prefix = ""): Row[] {
  if (!probes) return [];
  return keys.filter((k) => probes[k]).map((k) => {
    const p = probes[k];
    const [label, detail] = PROBE_LABELS[k] ?? [k, ""];
    const dc = k === "telegram" && p.session_dc != null
      ? `session on DC${p.session_dc}${p.nearest_dc != null && p.nearest_dc !== p.session_dc ? `, nearest is DC${p.nearest_dc}` : ""}`
      : "";
    return {
      key: `${prefix}probe-${k}`, label: `${prefix}${label}`, detail,
      p50: p.ms, p90: null, max: null, n: null,
      state: !p.ok ? "fail" : p.ms != null && p.ms > p.amber_ms ? "slow" : "ok",
      note: p.ok ? dc : p.detail,
    };
  });
}

export function brokerRows(broker: Broker | undefined, prefix = ""): Row[] {
  if (!broker) return [];
  if (!broker.available) {
    return [{
      key: `${prefix}broker-none`, label: `${prefix}Broker execution (MT5 logs)`,
      detail: "", p50: null, p90: null, max: null, n: null, state: "none",
      note: "no MetaTrader terminal logs found on this machine",
    }];
  }
  return Object.entries(broker.servers).map(([server, s]) => ({
    key: `${prefix}broker-${server}`,
    label: `${prefix}Broker execution: ${server}`,
    detail: `From the terminal's own logs ("done in N ms"), last ${broker.days ?? 7} days. Outside this app: the broker's server.`,
    p50: s.median_ms, p90: s.p90_ms, max: s.max_ms, n: s.n,
    state: s.slow ? "slow" : "ok",
    note: s.over_5s ? `${s.over_5s} took over 5 s` : "",
  }));
}

export function vpsLinkRow(vps: VpsReport): Row {
  return {
    key: "vps-link", label: "Mac ↔ VPS round trip",
    detail: "A ping over the sync link and its echo.",
    p50: vps.rtt_ms, p90: null, max: null, n: null,
    state: vps.rtt_ms == null ? "fail" : vps.rtt_ms > vps.amber_ms ? "slow" : "ok",
    note: vps.detail,
  };
}
