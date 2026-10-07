import type { Candle } from "@/api/types";

/** How close to the oldest bar, in bars, counts as "at the left edge". */
export const EDGE_BARS = 5;

/** Bars asked for per scroll-back page. The server caps it at 2000. */
export const HISTORY_PAGE = 500;

/**
 * Older bars in front of the live window, each once, oldest first
 * (docs/todo/011 phase 2).
 *
 * An older bar at or after the live window's first is dropped: the live poll
 * is the fresher copy, and lightweight-charts throws on a repeated or
 * out-of-order time rather than drawing it.
 */
export function mergeHistory(older: Candle[], live: Candle[]): Candle[] {
  const cut = live[0]?.ts ?? Infinity;
  const byTs = new Map<number, Candle>();
  for (const c of older) if (c.ts < cut) byTs.set(c.ts, c);
  if (byTs.size === 0) return live;
  return [...[...byTs.keys()].sort((a, b) => a - b).map((ts) => byTs.get(ts)!), ...live];
}
