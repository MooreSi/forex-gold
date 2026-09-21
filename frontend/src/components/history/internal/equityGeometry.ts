import type { CurvePoint } from "../hooks/useClosedTrades";

/**
 * The arithmetic behind the equity curve, kept out of the component so it can
 * be read — and tested — without rendering an SVG.
 *
 * The chart it replaced (NiceGUI + ECharts) had a money axis, dated category
 * labels and a gradient area; ECharts computed all of that. Drawn by hand,
 * the ticks are the part that has to be deliberate: an axis whose labels are
 * `123.4567` is worse than no axis, and one that omits zero hides the line
 * every figure here is measured from.
 */

/** Drawing box, in the SVG's own units. */
export const W = 720;
export const H = 220;
export const M = { top: 12, right: 58, bottom: 24, left: 8 };

export interface Tick { value: number; y: number; label: string }
export interface XTick { x: number; label: string }

export interface Geometry {
  line: string;
  area: string;
  zeroY: number;
  yTicks: Tick[];
  xTicks: XTick[];
  last: { x: number; y: number; value: number } | null;
}

/**
 * A short money label. `formatMoney` gives `$1,234.56`, which is the right
 * answer for a figure being read and the wrong one for a tick: six axis
 * labels of that width do not fit, and the decimals are noise at this scale.
 */
export function tickLabel(value: number): string {
  const rounded = Math.round(value);
  const abs = Math.abs(rounded).toLocaleString("en-GB");
  return `${rounded < 0 ? "-" : ""}$${abs}`;
}

/**
 * Round tick values covering [lo, hi], always including zero.
 *
 * 1/2/5 x 10^n, the standard choice, because the alternative — dividing the
 * range into n equal parts — produces labels like `$-83.3` that nobody can
 * read a value off.
 */
export function niceTicks(lo: number, hi: number, target = 4): number[] {
  const low = Math.min(0, lo);
  const high = Math.max(0, hi);
  const span = high - low;
  if (span <= 0) return [0];

  const raw = span / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;

  const ticks: number[] = [];
  for (let v = Math.ceil(low / step) * step; v <= high + step / 1000; v += step) {
    // Floating-point accumulation puts 1e-13 where a zero should be, and that
    // renders as "$0" in the label but not at the zero LINE's height.
    ticks.push(Math.abs(v) < step / 1000 ? 0 : v);
  }
  if (!ticks.includes(0)) ticks.push(0);
  return ticks.sort((a, b) => a - b);
}

export function buildGeometry(
  points: CurvePoint[], formatTs: (ts: number) => string,
): Geometry | null {
  if (!points.length) return null;

  const values = points.map((p) => p.pnl);
  const lo = Math.min(0, ...values);
  const hi = Math.max(0, ...values);
  const ticks = niceTicks(lo, hi);
  const top = Math.max(hi, ...ticks);
  const bottom = Math.min(lo, ...ticks);
  // A window that never moved would divide by zero; one unit of range keeps
  // the line flat and on screen instead.
  const span = top - bottom || 1;

  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const y = (v: number) => M.top + ((top - v) / span) * plotH;
  const x = (i: number) => (points.length === 1
    ? M.left + plotW / 2
    : M.left + (i / (points.length - 1)) * plotW);

  const line = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.pnl).toFixed(1)}`)
    .join(" ");

  // Closed back to the ZERO line rather than to the floor of the box. Filled
  // to the floor, a window that lost money shades exactly like one that made
  // money, which is the opposite of what the fill is for.
  const zeroY = y(0);
  const area = `${line} L${x(points.length - 1).toFixed(1)},${zeroY.toFixed(1)} `
    + `L${x(0).toFixed(1)},${zeroY.toFixed(1)} Z`;

  const lastIdx = points.length - 1;
  const xTickAt = points.length === 1
    ? [0]
    : [...new Set([0, Math.floor(lastIdx / 2), lastIdx])];

  return {
    line,
    area,
    zeroY,
    yTicks: ticks.map((v) => ({ value: v, y: y(v), label: tickLabel(v) })),
    xTicks: xTickAt.map((i) => ({ x: x(i), label: formatTs(points[i].ts) })),
    last: { x: x(lastIdx), y: y(points[lastIdx].pnl), value: points[lastIdx].pnl },
  };
}
