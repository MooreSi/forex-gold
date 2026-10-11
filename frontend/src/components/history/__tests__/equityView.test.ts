import { describe, expect, it } from "vitest";
import { sliceCurve } from "../internal/equityView";
import { buildGeometry } from "../internal/equityGeometry";

/**
 * The equity curve's time-window slider (owner, 2026-10-09).
 *
 * The backend's curve is realised P&L from zero across the loaded window.
 * Narrowing the view asks "what did the trading do between these two
 * times", so the view is REBASED: it starts at zero again, from where the
 * account stood just before the first trade in view. Drawing the raw
 * cumulative values would show a curve starting at +$80 and call it +$120,
 * when the trades in view made $40.
 */
const POINTS = [
  { ts: 100, pnl: 0 },    // the backend's zero anchor, at the first trade's time
  { ts: 100, pnl: 80 },
  { ts: 200, pnl: 30 },
  { ts: 300, pnl: 120 },
  { ts: 400, pnl: 90 },
];

describe("sliceCurve", () => {
  it("returns the backend's own curve when the whole window is in view", () => {
    const v = sliceCurve(POINTS, 100, 400)!;

    expect(v.points).toEqual(POINTS);
    expect(v.net).toBe(90);
    expect(v.peak).toBe(120);
    expect(v.maxDrawdown).toBe(50);
    expect(v.trades).toBe(4);
  });

  it("rebases a narrowed view to zero at the trade before it", () => {
    // Trades at 200 and 300: from 80 to 30 to 120.
    const v = sliceCurve(POINTS, 150, 350)!;

    expect(v.points.map((p) => p.pnl)).toEqual([0, -50, 40]);
    expect(v.net).toBe(40);
    expect(v.trades).toBe(2);
  });

  it("measures the drawdown inside the view from the view's own peak", () => {
    const v = sliceCurve(POINTS, 250, 400)!;

    // 30 -> 120 -> 90: up 90, back 30.
    expect(v.points.map((p) => p.pnl)).toEqual([0, 90, 60]);
    expect(v.peak).toBe(90);
    expect(v.maxDrawdown).toBe(30);
  });

  it("never reports a negative peak", () => {
    // A view that only lost: the best it got is where it started.
    const v = sliceCurve(POINTS, 150, 250)!;

    expect(v.net).toBe(-50);
    expect(v.peak).toBe(0);
    expect(v.maxDrawdown).toBe(50);
  });

  it("has nothing to draw for a window with no trades in it", () => {
    expect(sliceCurve(POINTS, 210, 290)).toBeNull();
    expect(sliceCurve([], 0, 1000)).toBeNull();
  });

  it("rounds to cents, so float sums do not print as $39.999999", () => {
    const v = sliceCurve([{ ts: 1, pnl: 0 }, { ts: 1, pnl: 0.1 }, { ts: 2, pnl: 0.3 }],
      2, 2)!;

    expect(v.net).toBe(0.2);
  });
});

describe("buildGeometry at a measured size", () => {
  const fmt = (ts: number) => String(ts);

  it("fills the width it is given, edge to edge of the plot", () => {
    const g = buildGeometry(POINTS, fmt, { w: 1400, h: 320 })!;

    expect(g.box).toEqual({ w: 1400, h: 320 });
    expect(g.points[0].x).toBeLessThan(20);
    expect(g.points[g.points.length - 1].x).toBeGreaterThan(1300);
  });

  it("puts more dates on a wider chart", () => {
    const narrow = buildGeometry(POINTS, fmt, { w: 360, h: 220 })!;
    const wide = buildGeometry(POINTS, fmt, { w: 1400, h: 220 })!;

    expect(wide.xTicks.length).toBeGreaterThan(narrow.xTicks.length);
  });

  it("finds the nearest point to a pointer position", () => {
    const g = buildGeometry(POINTS, fmt, { w: 1000, h: 220 })!;

    expect(g.indexAt(0)).toBe(0);
    expect(g.indexAt(g.points[3].x + 1)).toBe(3);
    expect(g.indexAt(5000)).toBe(POINTS.length - 1);
  });

  it("marks the peak and the bottom of the worst drawdown", () => {
    const g = buildGeometry(POINTS, fmt, { w: 1000, h: 220 })!;

    expect(g.peakIndex).toBe(3);
    // The worst fall from a running peak is 80 -> 30, ending at index 2.
    expect(g.troughIndex).toBe(2);
  });

  it("has no drawdown marker for a curve that never fell", () => {
    const g = buildGeometry([{ ts: 1, pnl: 0 }, { ts: 1, pnl: 10 }, { ts: 2, pnl: 20 }],
      fmt, { w: 1000, h: 220 })!;

    expect(g.troughIndex).toBeNull();
  });
});
