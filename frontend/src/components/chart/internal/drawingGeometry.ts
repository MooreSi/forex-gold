/**
 * Maths for the Broker chart's drawings (docs/todo/011). Pure, so it is
 * tested without a canvas.
 *
 * Drawings are stored as (time, price). lightweight-charts positions things
 * by bar index ("logical"), and the market has gaps, so a time becomes an
 * index through the bars actually loaded: interpolated between two bars,
 * and at the timeframe's spacing beyond either end (a line drawn into the
 * future, or back past the oldest bar loaded).
 */

export type DrawingKind = "trend" | "hline" | "rect" | "fib";

export interface DrawingPoint {
  time: number;
  price: number;
}

export interface Drawing {
  id: number;
  symbol: string;
  kind: DrawingKind;
  points: DrawingPoint[];
}

/** How many clicks each tool takes. Mirrors the API's POINTS_PER_KIND. */
export const POINTS_PER_KIND: Record<DrawingKind, number> = {
  trend: 2, hline: 1, rect: 2, fib: 2,
};

export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1] as const;

/** First index whose time is >= t, or times.length. */
function lowerBound(times: number[], t: number): number {
  let lo = 0;
  let hi = times.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export function timeToLogical(times: number[], tf: number, t: number): number {
  const n = times.length;
  if (n === 0) return 0;
  if (t <= times[0]) return (t - times[0]) / tf;
  if (t >= times[n - 1]) return n - 1 + (t - times[n - 1]) / tf;
  const i = lowerBound(times, t);
  if (times[i] === t) return i;
  const a = times[i - 1];
  const b = times[i];
  return i - 1 + (t - a) / (b - a);
}

export function logicalToTime(times: number[], tf: number, l: number): number {
  const n = times.length;
  if (n === 0) return 0;
  if (l <= 0) return times[0] + l * tf;
  if (l >= n - 1) return times[n - 1] + (l - (n - 1)) * tf;
  const i = Math.floor(l);
  const frac = l - i;
  return times[i] + frac * (times[i + 1] - times[i]);
}

/** The retracement levels of a swing drawn from `start` to `end`: 0% at the
 *  end, 100% back at the start, the way TradingView draws them. */
export function fibPrices(start: number, end: number): { level: number; price: number }[] {
  return FIB_LEVELS.map((level) => ({ level, price: end + (start - end) * level }));
}
