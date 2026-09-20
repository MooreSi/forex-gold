/**
 * The maths behind the "is it learning?" chart.
 *
 * The NiceGUI original (frontend/components/learning_chart.py) carries the
 * finding this replaces: both engine panels plotted a CUMULATIVE mean, and
 * over the Reversal engine's 4,880 labelled signals that mean is a constant
 * with extra steps -- point 4,880 moves it by 0.02%. It cannot answer "is it
 * learning?" because it weighs a signal from July exactly as heavily as one
 * from this morning. The rolling window is the fix, and these tests are what
 * stop it regressing to a cumulative mean again.
 */
import { describe, expect, it } from "vitest";
import { plotPoints, rollingMean, WINDOW } from "../internal/learning_series";

describe("the rolling mean", () => {
  it("averages the last WINDOW values, not the whole history", () => {
    // Fifty losses then fifty wins ends at 100%, where a cumulative mean
    // would still be sitting at 50% and look like nothing happened.
    const series = [...Array(WINDOW).fill(0), ...Array(WINDOW).fill(1)];

    const out = rollingMean(series);

    expect(out[out.length - 1]).toBeCloseTo(1, 5);
  });

  it("averages what there is before a full window exists", () => {
    // Padding with zeros would draw a fake climb out of the floor across
    // every engine's first fifty signals.
    expect(rollingMean([1, 1, 1])).toEqual([1, 1, 1]);
  });

  it("carries the last value through a gap rather than dropping to zero", () => {
    const out = rollingMean([1, null, 1]);

    expect(out[1]).toBe(out[0]);
  });

  it("starts at zero when the very first value is missing", () => {
    expect(rollingMean([null])).toEqual([0]);
  });

  it("gives an empty history an empty curve", () => {
    expect(rollingMean([])).toEqual([]);
  });
});

describe("plotting", () => {
  it("puts the newest value at the right-hand edge", () => {
    const pts = plotPoints([0, 1], 0, 1, 100, 50);

    expect(pts).toBe("0,50 100,0");
  });

  it("puts a value at the top of the range at the top of the box", () => {
    // Screen y grows downwards. Getting this backwards draws a learning
    // engine as a failing one.
    const pts = plotPoints([1], 0, 1, 100, 50);

    expect(pts).toBe("0,0");
  });

  it("plots only the tail, so the line has visible shape", () => {
    // 4,880 points in 280 pixels is 17 points per pixel: a line that cannot
    // move when a signal closes.
    const many = Array.from({ length: 500 }, (_, i) => i / 500);

    const count = plotPoints(many, 0, 1, 100, 50).split(" ").length;

    expect(count).toBeLessThan(500);
  });

  it("draws nothing for an empty series rather than a flat line at zero", () => {
    // A flat line at the bottom of the box is a measurement. Nothing is not.
    expect(plotPoints([], 0, 1, 100, 50)).toBe("");
  });

  it("draws nothing when the range is a single value", () => {
    expect(plotPoints([1, 2], 5, 5, 100, 50)).toBe("");
  });

  it("clamps a value beyond the stated range into the box", () => {
    // Realised R is plotted on -1..+1 and a single +3R trade exists. Left
    // unclamped it draws outside the SVG and the line vanishes.
    const pts = plotPoints([3], -1, 1, 100, 50);

    expect(pts).toBe("0,0");
  });
});
