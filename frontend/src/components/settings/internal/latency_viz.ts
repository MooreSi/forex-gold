/**
 * Settings > Latency: the drawing rules every picture on the tab shares.
 *
 * One log scale for the whole tab, so a bar on one checker can be compared
 * with a bar on the other by eye. Delays here run from a tenth of a
 * millisecond (the event queue) to two minutes (the worst broker fill); on a
 * linear axis everything but the broker would be a single pixel.
 */
import type { Hop, PipelineView, RowState } from "./latency_rows";

const LOG_MIN = Math.log10(0.1);
const LOG_MAX = Math.log10(200_000);

/** Gridlines, in ms, with the label printed under each. */
export const TICKS: [number, string][] = [
  [1, "1ms"], [10, "10ms"], [100, "100ms"], [1000, "1s"], [10_000, "10s"], [100_000, "100s"],
];

/** 0..100: where a delay sits on the tab's log axis. */
export function logPct(ms: number | null | undefined): number {
  if (ms == null || !Number.isFinite(ms)) return 0;
  const v = Math.log10(Math.max(ms, 0.1));
  return Math.min(100, Math.max(0, ((v - LOG_MIN) / (LOG_MAX - LOG_MIN)) * 100));
}

export const STATE_TEXT: Record<RowState, string> = {
  ok: "text-profit",
  slow: "text-warning",
  fail: "text-loss",
  none: "text-ink-3",
};

export const STATE_BG: Record<RowState, string> = {
  ok: "bg-profit",
  slow: "bg-warning",
  fail: "bg-loss",
  none: "bg-ink-3",
};

export const STATE_WORD: Record<RowState, string> = {
  ok: "healthy",
  slow: "slow",
  fail: "no answer",
  none: "not measured",
};

export function hopState(h: Hop): RowState {
  if (h.stats?.n == null) return "none";
  return h.slow ? "slow" : "ok";
}

/** The legs a signal travels, without the "total" row that sums them. */
export function legs(view: PipelineView | undefined): Hop[] {
  return (view?.hops ?? []).filter((h) => h.id !== "total");
}

export interface EndToEnd {
  ms: number | null;
  /** true when this is the backend's own measured total, not a sum of legs. */
  measured: boolean;
  unmeasured: number;
  legs: number;
  slow: boolean;
}

/**
 * The typical time from one end of a checker to the other.
 *
 * The backend's own "total" hop wins when it has been measured. Otherwise it
 * is the sum of the measured legs' medians, and the count of legs NOT in it
 * travels with it: a sum that skips the order leg is not a signal-to-fill
 * time, and the tile says so rather than presenting it as one.
 */
export function endToEnd(view: PipelineView | undefined): EndToEnd {
  const all = legs(view);
  const total = view?.hops.find((h) => h.id === "total");
  const measured = all.filter((h) => h.stats?.n != null);
  const slow = measured.some((h) => h.slow);
  if (total?.stats?.n != null && total.stats.p50 != null) {
    return { ms: total.stats.p50, measured: true, unmeasured: 0, legs: all.length, slow: total.slow };
  }
  const sum = measured.reduce((a, h) => a + (h.stats.p50 ?? 0), 0);
  return {
    ms: measured.length ? sum : null,
    measured: false,
    unmeasured: all.length - measured.length,
    legs: all.length,
    slow,
  };
}
