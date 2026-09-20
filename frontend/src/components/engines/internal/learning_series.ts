/**
 * The maths behind the "is it learning?" chart.
 *
 * Ported from the NiceGUI `frontend/components/learning_chart.py`, including
 * the reason it exists. Both engine panels used to plot a **cumulative** mean:
 *
 *     win_rate[i] = wins / (i + 1) * 100
 *
 * The Reversal engine has thousands of labelled signals. Point 4,880 moves
 * that mean by about 0.02%, so the line is a constant with extra steps — and
 * it cannot answer "is it learning?" at all, because it weighs a signal from
 * July exactly as heavily as one from this morning.
 *
 * The two series drawn with these helpers are NOT comparable to each other.
 * Win rate is 0-100% and mean realised R is -1..+1; each is read against the
 * midline, which the chart says on its own face.
 */

/** Rolling window, in closed signals. Long enough that one trade does not
 *  swing the line, short enough that a month-old trade has left it. */
export const WINDOW = 50;

/** How many rolling values to draw. The mean is computed over the whole
 *  history; only the tail is plotted, so the line has visible shape and moves
 *  when a signal closes instead of being 17 points deep per pixel. */
export const PLOT_POINTS = 120;

/**
 * Mean of the last `window` values at each point.
 *
 * Before `window` points exist it averages what there is, rather than padding
 * with zeros — padding would draw a fake climb out of the floor across every
 * engine's first fifty signals.
 */
export function rollingMean(
  series: (number | null | undefined)[], window = WINDOW,
): number[] {
  const out: number[] = [];
  const run: number[] = [];
  for (const v of series) {
    if (v == null || !Number.isFinite(Number(v))) {
      // A gap is a signal whose outcome is not known, not a zero. Zero here
      // is a loss the engine never had.
      out.push(out.length > 0 ? out[out.length - 1] : 0);
      continue;
    }
    run.push(Number(v));
    if (run.length > window) run.shift();
    out.push(run.reduce((a, b) => a + b, 0) / run.length);
  }
  return out;
}

/**
 * The tail of `series` as SVG polyline points, scaled into `lo`..`hi`.
 *
 * Values outside the range are clamped rather than dropped: a single +3R
 * trade on a -1..+1 axis would otherwise draw off the top of the box and take
 * the visible part of the line with it.
 */
export function plotPoints(
  series: (number | null | undefined)[], lo: number, hi: number,
  w: number, h: number,
): string {
  if (series.length === 0 || hi === lo) return "";
  const tail = series.slice(-PLOT_POINTS);
  const pts: string[] = [];
  tail.forEach((v, i) => {
    if (v == null || !Number.isFinite(Number(v))) return;
    const clamped = Math.max(lo, Math.min(hi, Number(v)));
    const x = Math.round((i / Math.max(tail.length - 1, 1)) * w);
    // Screen y grows downwards, so the top of the range is y = 0. Getting
    // this the wrong way round draws a learning engine as a failing one.
    const y = Math.round(h - ((clamped - lo) / (hi - lo)) * h);
    pts.push(`${x},${y}`);
  });
  return pts.join(" ");
}
