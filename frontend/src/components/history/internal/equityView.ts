import type { CurvePoint } from "../hooks/useClosedTrades";

/**
 * The part of the equity curve between two times, and its figures.
 *
 * The backend's curve is `[{ts, pnl: 0}, ...one point per trade]`: a zero
 * anchor at the first trade's time, then the running realised total. A view
 * of it is REBASED to start at zero again, from where the account stood just
 * before the first trade in view. Drawing the raw running totals would show
 * a view that starts at +$80 and ends at +$120 and call it +$120, when the
 * trades in view made $40.
 *
 * With the whole window in view the result is the backend's curve exactly,
 * and its figures are the backend's figures.
 */
export interface CurveView {
  points: CurvePoint[];
  net: number;
  peak: number;
  maxDrawdown: number;
  trades: number;
}

const cents = (v: number) => Math.round(v * 100) / 100;

export function sliceCurve(
  points: CurvePoint[], fromTs: number, toTs: number,
): CurveView | null {
  // Index 0 is the anchor, not a trade.
  const inView: number[] = [];
  for (let i = 1; i < points.length; i++) {
    if (points[i].ts >= fromTs && points[i].ts <= toTs) inView.push(i);
  }
  if (!inView.length) return null;

  const first = inView[0];
  const base = points[first - 1].pnl;
  const out: CurvePoint[] = [{ ts: points[first].ts, pnl: 0 }];
  let peak = 0;
  let maxDrawdown = 0;
  for (const i of inView) {
    const v = cents(points[i].pnl - base);
    peak = Math.max(peak, v);
    maxDrawdown = Math.max(maxDrawdown, peak - v);
    out.push({ ts: points[i].ts, pnl: v });
  }

  return {
    points: out,
    net: out[out.length - 1].pnl,
    peak: cents(peak),
    maxDrawdown: cents(maxDrawdown),
    trades: inView.length,
  };
}
