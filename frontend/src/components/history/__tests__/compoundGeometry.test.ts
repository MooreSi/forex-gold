import { describe, expect, it } from "vitest";
import { axisMoney, buildBalanceChart, buildBars, logTicks } from "../internal/compoundGeometry";

/**
 * The Compound Calculator chart's axes.
 *
 * Compounding is a hockey stick: at 1% a day a year ends at 13x, at 3% at
 * 2,000x. On a linear axis everything before the last few months is a flat
 * line on the floor, so the log axis is the one that has to be right -- its
 * ticks have to be round numbers, in range, and few enough to read.
 */
describe("logTicks", () => {
  it("uses 1, 2 and 5 of each power of ten inside the range", () => {
    expect(logTicks(1000, 13000)).toEqual([1000, 2000, 5000, 10000]);
  });

  it("keeps only the powers of ten when the range is wide", () => {
    expect(logTicks(1000, 50_000_000)).toEqual([1000, 10_000, 100_000, 1_000_000, 10_000_000]);
  });

  it("never offers a tick outside the range", () => {
    for (const t of logTicks(1500, 7200)) {
      expect(t).toBeGreaterThanOrEqual(1500);
      expect(t).toBeLessThanOrEqual(7200);
    }
  });
});

describe("axisMoney", () => {
  it("keeps small figures whole and shortens large ones", () => {
    expect(axisMoney(2500)).toBe("$2,500");
    expect(axisMoney(13_290_000)).toBe("$13.29M");
  });
});

describe("buildBalanceChart", () => {
  const series = {
    goal: [1000, 1100, 1210, 1331],
    half: [1000, 1050, 1102.5, 1157.6],
    paid: [1000, 1000, 1000, 1000],
  };

  it("draws the highest value at the top of the plot on both scales", () => {
    for (const log of [false, true]) {
      const g = buildBalanceChart(series, [3], log);
      const ys = g.points.goal.map((p) => p.y);
      expect(Math.min(...ys)).toBe(ys[3]);
    }
  });

  it("does not put zero on a log axis", () => {
    const g = buildBalanceChart(series, [3], true);

    expect(g.yTicks.every((t) => t.value > 0)).toBe(true);
  });

  it("labels the months along the bottom", () => {
    const g = buildBalanceChart(series, [1, 2, 3], false);

    expect(g.xTicks.map((t) => t.label)).toEqual(["M1", "M2", "M3"]);
  });
});

describe("the profit bars", () => {
  it("hangs a losing period below the zero line and a winning one above it", () => {
    const chart = buildBars([100, -50], "Week");
    const zero = chart.yTicks.find((t) => t.value === 0)!.y;
    const [win, loss] = chart.bars;

    expect(win.y + win.h).toBeCloseTo(zero, 6);
    expect(loss.y).toBeCloseTo(zero, 6);
    expect(loss.h).toBeCloseTo(win.h / 2, 6);
    // Inside the chart, not drawn off its bottom edge.
    expect(loss.y + loss.h).toBeLessThanOrEqual(chart.box.h);
    expect(chart.yTicks.some((t) => t.value < 0)).toBe(true);
  });
});
