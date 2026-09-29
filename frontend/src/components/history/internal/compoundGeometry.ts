import { formatCompactMoney } from "@/components/shared/format";
import { niceTicks, tickLabel } from "./equityGeometry";

/**
 * The Compound Calculator's chart arithmetic, kept out of the component so it
 * can be tested without rendering an SVG.
 *
 * The drawing box is wider than the equity curve's. These charts are drawn
 * full width with a height that follows from the box, and at the equity
 * curve's 720x220 a wide screen made them half a screen tall with axis labels
 * to match.
 *
 * **Why a log axis at all.** Compounding is a hockey stick: 1% a day is 13x
 * in a year, 3% a day is 2,000x. On a linear axis the first nine months lie
 * flat on the floor and the half-goal line is invisible beside the goal. On a
 * log axis a constant rate is a straight line, which is the honest picture of
 * "the same percentage every day".
 */

/** Drawing box, in the SVG's own units. */
export const W = 1000;
export const H = 280;
export const M = { top: 14, right: 64, bottom: 26, left: 10 };

export interface Pt { x: number; y: number }
export interface YTick { value: number; y: number; label: string }
export interface XTick { x: number; label: string }

export interface BalanceSeries { goal: number[]; half: number[]; paid: number[] }

export interface BalanceChart {
  points: Record<keyof BalanceSeries, Pt[]>;
  paths: Record<keyof BalanceSeries, string>;
  goalArea: string;
  yTicks: YTick[];
  xTicks: XTick[];
  /** The trading day under an x position, for the hover readout. */
  dayAt: (x: number) => number;
  plot: { left: number; right: number; top: number; bottom: number };
}

/** An axis label: whole dollars while it fits, `$13.29M` once it does not. */
export function axisMoney(value: number): string {
  if (Math.abs(value) < 10_000) return tickLabel(value);
  return formatCompactMoney(value).replace(/\.00(?=[KMBT])/, "");
}

/** Round ticks inside [lo, hi] for a log axis. Both ends must be above zero. */
export function logTicks(lo: number, hi: number): number[] {
  const out: number[] = [];
  for (let p = Math.floor(Math.log10(lo)); p <= Math.ceil(Math.log10(hi)); p++) {
    for (const m of [1, 2, 5]) {
      const v = m * 10 ** p;
      if (v >= lo && v <= hi) out.push(v);
    }
  }
  if (out.length > 6) {
    const powers = out.filter((v) => Math.abs(Math.log10(v) % 1) < 1e-9);
    if (powers.length >= 2) return powers;
  }
  if (out.length >= 2) return out;
  // A narrow range with at most one round number in it: fall back to evenly
  // stepped ticks, which on a narrow log axis are all but evenly spaced.
  return niceTicks(lo, hi).filter((v) => v >= lo && v <= hi);
}

function pathOf(points: Pt[]): string {
  return points
    .map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`)
    .join(" ");
}

export function buildBalanceChart(
  series: BalanceSeries, monthEnds: number[], log: boolean,
): BalanceChart {
  const all = [...series.goal, ...series.half, ...series.paid];
  const n = series.goal.length;
  const hi = Math.max(...all);
  const lo = Math.min(...all);

  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const x = (i: number) => M.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);

  let y: (v: number) => number;
  let yTicks: number[];
  if (log) {
    // A little headroom either side, so neither line sits on the frame.
    const top = Math.log10(hi) + 0.02;
    const bottom = Math.log10(lo) - 0.02;
    const span = top - bottom || 1;
    y = (v) => M.top + ((top - Math.log10(v)) / span) * plotH;
    yTicks = logTicks(lo, hi);
  } else {
    yTicks = niceTicks(0, hi);
    const top = Math.max(hi, ...yTicks);
    y = (v) => M.top + ((top - v) / (top || 1)) * plotH;
  }

  const pts = (vs: number[]) => vs.map((v, i) => ({ x: x(i), y: y(v) }));
  const points = { goal: pts(series.goal), half: pts(series.half), paid: pts(series.paid) };

  // Filled down to the money put in, not to the floor: the shaded band is
  // then exactly the profit, which is what the chart is about.
  const paidBack = [...points.paid].reverse()
    .map((p) => `L${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const goalArea = `${pathOf(points.goal)} ${paidBack} Z`;

  const step = Math.max(1, Math.ceil(monthEnds.length / 8));
  const xTicks = monthEnds
    .map((day, i) => ({ day, m: i + 1 }))
    .filter(({ m }) => m % step === 0 || m === monthEnds.length)
    .filter(({ m }, i, arr) => !(i === arr.length - 2 && monthEnds.length - m < step / 2))
    .map(({ day, m }) => ({ x: x(Math.min(day, n - 1)), label: `M${m}` }));

  return {
    points,
    paths: { goal: pathOf(points.goal), half: pathOf(points.half), paid: pathOf(points.paid) },
    goalArea,
    yTicks: yTicks.map((v) => ({ value: v, y: y(v), label: axisMoney(v) })),
    xTicks,
    dayAt: (px: number) => Math.max(0, Math.min(n - 1,
      Math.round(((px - M.left) / plotW) * (n - 1)))),
    plot: { left: M.left, right: W - M.right, top: M.top, bottom: H - M.bottom },
  };
}

export interface Bar { x: number; y: number; w: number; h: number; value: number; label: string }

/** One bar per period, for the profit-per-week or per-month chart. */
export function buildBars(values: number[], prefix: string): { bars: Bar[]; yTicks: YTick[] } {
  const hi = Math.max(0, ...values);
  const ticks = niceTicks(0, hi);
  const top = Math.max(hi, ...ticks) || 1;
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const slot = plotW / Math.max(values.length, 1);
  const w = Math.max(1, slot * 0.72);
  const y = (v: number) => M.top + ((top - v) / top) * plotH;
  return {
    bars: values.map((v, i) => ({
      x: M.left + i * slot + (slot - w) / 2,
      y: y(Math.max(v, 0)),
      w,
      h: Math.abs(y(v) - y(0)),
      value: v,
      label: `${prefix} ${i + 1}`,
    })),
    yTicks: ticks.map((v) => ({ value: v, y: y(v), label: axisMoney(v) })),
  };
}
