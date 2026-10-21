/**
 * Where economic-calendar events go on the Broker chart (the NEWS button).
 * Pure, so it is tested without a canvas.
 *
 * An event's `ts` is a real UTC instant; a candle's is broker time written as
 * if it were UTC (backend/src/services/market/bar_clock.py). The broker's
 * midnight is New York's 17:00, so its clock is New York's plus seven hours:
 * UTC+3 in the US summer and UTC+2 in its winter. Worked out per event, so a
 * release from before a clock change lands on the bar MT5 stamped then. A
 * fresh tick that disagrees with the rule (a broker on another clock) shifts
 * every event by the difference.
 */
import type { Candle } from "@/api/types";

export type NewsImpact = "high" | "medium" | "low";

export interface ChartNewsEvent {
  ts: number;
  title: string;
  currency: string;
  impact: string;
  forecast?: string;
  previous?: string;
}

export interface NewsMark {
  /** The bar's own (broker-time) stamp. */
  barTime: number;
  /** The strongest impact among `events`. */
  impact: NewsImpact;
  /** Strongest first. */
  events: ChartNewsEvent[];
}

export interface NewsFilter {
  impacts: Set<string>;
  /** False keeps gold's currencies only, as the news blackout does. */
  allCurrencies: boolean;
}

export const NEWS_IMPACTS: NewsImpact[] = ["high", "medium", "low"];
export const GOLD_CURRENCIES = new Set(["USD", "XAU"]);

const RANK: Record<string, number> = { high: 3, medium: 2, low: 1 };
const HOUR = 3600;
// Mirrors bar_clock.py: how old a tick may be and still say which hour it is.
const MAX_STALE_S = 1800;
const SKEW_S = 60;
const MAX_OFFSET_S = 14 * HOUR;

const NEW_YORK = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hourCycle: "h23",
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
});

function newYorkOffsetS(utcS: number): number {
  const p: Record<string, number> = {};
  for (const part of NEW_YORK.formatToParts(new Date(utcS * 1000))) {
    if (part.type !== "literal") p[part.type] = Number(part.value);
  }
  const wall = Date.UTC(p.year, p.month - 1, p.day, p.hour % 24, p.minute, p.second) / 1000;
  return Math.round((wall - Math.floor(utcS)) / 60) * 60;
}

/** Seconds the broker clock runs ahead of UTC at a UTC instant, by the rule. */
export function brokerOffsetS(utcS: number): number {
  return newYorkOffsetS(utcS) + 7 * HOUR;
}

/** How far a fresh tick says the broker is from the rule; 0 when no tick
 *  is fresh enough to say (the weekend, a stopped bridge). */
export function measuredCorrectionS(tickTs: number | null | undefined, nowS: number): number {
  if (!tickTs) return 0;
  const raw = tickTs - nowS;
  const off = Math.ceil((raw - SKEW_S) / HOUR) * HOUR;
  if (off - raw >= MAX_STALE_S || Math.abs(off) > MAX_OFFSET_S) return 0;
  return off - brokerOffsetS(nowS);
}

/** The last index whose time is <= t, or -1. */
function barAtOrBefore(times: number[], t: number): number {
  let lo = 0;
  let hi = times.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= t) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

/** Events grouped onto the bars they fell in. An event in no loaded bar (before
 *  the first, after the last has closed, in a market gap) is left out. */
export function placeNews(
  events: ChartNewsEvent[], times: number[], tfSeconds: number,
  correctionS: number, filter: NewsFilter,
): NewsMark[] {
  const byBar = new Map<number, ChartNewsEvent[]>();
  for (const e of events) {
    if (!filter.impacts.has(e.impact)) continue;
    if (!filter.allCurrencies && !GOLD_CURRENCIES.has(e.currency)) continue;
    const at = e.ts + brokerOffsetS(e.ts) + correctionS;
    const i = barAtOrBefore(times, at);
    if (i < 0 || at >= times[i] + tfSeconds) continue;
    const list = byBar.get(times[i]) ?? [];
    list.push(e);
    byBar.set(times[i], list);
  }
  return [...byBar.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([barTime, list]) => {
      const sorted = [...list].sort((a, b) => (RANK[b.impact] ?? 0) - (RANK[a.impact] ?? 0));
      return { barTime, impact: sorted[0].impact as NewsImpact, events: sorted };
    });
}

/** What price did from the event bar's open to the close half an hour on (one
 *  bar on longer timeframes), clipped to the bars loaded. */
export function moveAfter(
  bars: Candle[], barTime: number, tfSeconds: number,
): { change: number; range: number; bars: number } | null {
  const i = bars.findIndex((b) => b.ts === barTime);
  if (i < 0) return null;
  const span = bars.slice(i, i + Math.max(1, Math.floor(1800 / tfSeconds)));
  const last = span[span.length - 1];
  return {
    change: Number((last.close - span[0].open).toFixed(5)),
    range: Number((Math.max(...span.map((b) => b.high)) - Math.min(...span.map((b) => b.low))).toFixed(5)),
    bars: span.length,
  };
}

/** Seconds per bar, read off the bars: the smallest step between two. */
export function barSeconds(times: number[]): number {
  let best = Infinity;
  for (let i = 1; i < times.length; i++) {
    const step = times[i] - times[i - 1];
    if (step > 0 && step < best) best = step;
  }
  return Number.isFinite(best) ? best : 60;
}
