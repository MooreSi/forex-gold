import type { Candle } from "@/api/types";

/**
 * The small amount of arithmetic the Dashboard does for itself, kept out of
 * the components so it can be read and tested without rendering anything.
 *
 * It is deliberately small. Every figure this screen shows is measured by the
 * backend — win rate, profit factor, drawdown, the curve, the biases, the
 * zones — and re-deriving any of them here would be a second answer to a
 * question something else has already answered. What is left is the two
 * things that only exist on this screen: today's move, and the shape of a
 * sparkline.
 */

export interface DayChange {
  absolute: number;
  percent: number;
}

/**
 * Today's move: the live mid against the open of the day that is forming.
 *
 * The reference is the DAILY OPEN, not the previous close and not the first
 * candle of whatever window the chart happens to be showing. Those are
 * different numbers, each of them plausible, and a price header that quietly
 * changes which one it means is unreadable.
 *
 * Null when either side is missing. A day nobody could measure is not a flat
 * day, and "+0.00 (0.00%)" is the most confident possible way to say nothing.
 */
export function dayChange(mid: number | null | undefined,
                          dailyCandles: Candle[]): DayChange | null {
  if (mid == null || !Number.isFinite(mid)) return null;
  // The LAST bar is the one forming now; the request asks for two so that a
  // feed which has not yet opened today still has yesterday to fall back on.
  const today = dailyCandles[dailyCandles.length - 1];
  const open = today?.open;
  if (open == null || !Number.isFinite(open) || open === 0) return null;
  const absolute = mid - open;
  return { absolute, percent: (absolute / open) * 100 };
}

/**
 * An SVG path through a series, scaled to a box.
 *
 * Not the Analysis tab's equity chart, which has a money axis, dated ticks and
 * a filled area — this has no axis at all, because a sparkline's job is the
 * SHAPE and a card this size has no room to label anything. The figures beside
 * it are what carry the values.
 *
 * Returns null for fewer than two points: a single point draws a dot that
 * reads as a flat line, which is a claim about a series nobody has.
 */
export function sparkPath(values: number[], width: number, height: number,
                          pad = 2): string | null {
  if (values.length < 2) return null;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const usable = height - pad * 2;
  const step = width / (values.length - 1);
  return values
    .map((v, i) => {
      const x = i * step;
      const y = pad + (1 - (v - lo) / span) * usable;
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
}
