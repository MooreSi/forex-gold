/**
 * Where a stored drawing lands on screen (docs/todo/011).
 *
 * Drawings are stored as (time, price) so they stay on the same candle and
 * price whatever the zoom. The chart plots by BAR INDEX ("logical"), not by
 * time, and a market has gaps (nights, weekends), so time has to be turned
 * into an index through the bars actually on the chart. Past the last bar, a
 * line drawn into the future keeps the timeframe's spacing.
 */
import { describe, expect, it } from "vitest";
import {
  FIB_LEVELS, fibPrices, logicalToTime, positionLevels, timeToLogical,
} from "../internal/drawingGeometry";

const TF = 300; // 5m
// Four bars with a weekend-sized gap between the second and third.
const TIMES = [1000 * TF, 1001 * TF, 1500 * TF, 1501 * TF];

describe("timeToLogical", () => {
  it("is the bar's index for a bar's own time", () => {
    expect(TIMES.map((t) => timeToLogical(TIMES, TF, t))).toEqual([0, 1, 2, 3]);
  });

  it("interpolates inside a gap rather than by clock time", () => {
    // Halfway through the weekend is halfway between bar 1 and bar 2.
    expect(timeToLogical(TIMES, TF, (TIMES[1] + TIMES[2]) / 2)).toBeCloseTo(1.5, 9);
  });

  it("carries on at the timeframe's spacing past the last bar", () => {
    expect(timeToLogical(TIMES, TF, TIMES[3] + 10 * TF)).toBe(13);
  });

  it("carries on at the timeframe's spacing before the first bar", () => {
    expect(timeToLogical(TIMES, TF, TIMES[0] - 4 * TF)).toBe(-4);
  });
});

describe("logicalToTime", () => {
  it("undoes timeToLogical, in the gap and past both ends", () => {
    for (const l of [-4, 0, 0.5, 1.5, 3, 13]) {
      expect(timeToLogical(TIMES, TF, logicalToTime(TIMES, TF, l))).toBeCloseTo(l, 9);
    }
  });
});

describe("fibPrices", () => {
  it("puts 0% at the end of the swing and 100% at its start", () => {
    // Drawn from a low of 4100 up to a high of 4200.
    const levels = fibPrices(4100, 4200);
    expect(levels[0]).toEqual({ level: 0, price: 4200 });
    expect(levels[levels.length - 1]).toEqual({ level: 1, price: 4100 });
  });

  it("puts the 61.8% retracement 61.8% of the way back", () => {
    const at618 = fibPrices(4100, 4200).find((l) => l.level === 0.618);
    expect(at618?.price).toBeCloseTo(4200 - 61.8, 9);
  });

  it("works the same way down a falling swing", () => {
    const at382 = fibPrices(4200, 4100).find((l) => l.level === 0.382);
    expect(at382?.price).toBeCloseTo(4100 + 38.2, 9);
  });

  it("draws the usual seven levels", () => {
    expect(FIB_LEVELS).toEqual([0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]);
  });
});

describe("positionLevels", () => {
  // Entry, stop, target: the order the three clicks are made in.
  const at = (price: number) => ({ time: TIMES[0], price });

  it("is a BUY when the stop is under the entry and the target over it", () => {
    expect(positionLevels([at(4150), at(4140), at(4170)])).toEqual({
      direction: "BUY", entry: 4150, stop: 4140, target: 4170, risk: 10, reward: 20, rr: 2,
    });
  });

  it("is a SELL when the stop is over the entry and the target under it", () => {
    const p = positionLevels([at(4150), at(4155), at(4135)]);
    expect(p?.direction).toBe("SELL");
    expect(p?.rr).toBe(3);
  });

  it("is not a position when the stop and target are on the same side", () => {
    // Which way would it trade? Guessing is how an order goes the wrong way.
    expect(positionLevels([at(4150), at(4140), at(4145)])).toBeNull();
  });

  it("is not a position when the stop is at the entry", () => {
    expect(positionLevels([at(4150), at(4150), at(4170)])).toBeNull();
  });

  it("is not a position until all three points exist", () => {
    expect(positionLevels([at(4150), at(4140)])).toBeNull();
  });
});
