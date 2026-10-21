import { describe, expect, it } from "vitest";
import {
  barSeconds, brokerOffsetS, measuredCorrectionS, moveAfter, placeNews, type ChartNewsEvent,
} from "../internal/newsMarks";

const H = 3600;
const utc = (...a: [number, number, number, number?, number?]) =>
  Date.UTC(a[0], a[1] - 1, a[2], a[3] ?? 0, a[4] ?? 0) / 1000;

const ev = (ts: number, impact: string, title = "CPI", currency = "USD"): ChartNewsEvent =>
  ({ ts, impact, title, currency, forecast: "", previous: "" });

describe("the broker clock", () => {
  // New York 17:00 is the broker's midnight (backend/src/services/market/bar_clock.py).
  it("is three hours ahead of UTC in the US summer", () => {
    expect(brokerOffsetS(utc(2026, 7, 1, 12))).toBe(3 * H);
  });

  it("is two hours ahead in the US winter", () => {
    expect(brokerOffsetS(utc(2026, 12, 1, 12))).toBe(2 * H);
  });

  it("follows New York's change, not London's", () => {
    // 2026-03-10: New York has moved to EDT, London is still on GMT.
    expect(brokerOffsetS(utc(2026, 3, 10, 12))).toBe(3 * H);
  });
});

describe("the measured correction", () => {
  const now = utc(2026, 7, 1, 12);

  it("is zero when a fresh tick agrees with the rule", () => {
    expect(measuredCorrectionS(now + 3 * H - 2, now)).toBe(0);
  });

  it("is the difference when the broker runs another clock", () => {
    expect(measuredCorrectionS(now - 1, now)).toBe(-3 * H);
  });

  it("is zero when the tick is too old to tell (a weekend)", () => {
    expect(measuredCorrectionS(now - 40 * H, now)).toBe(0);
    expect(measuredCorrectionS(null, now)).toBe(0);
  });
});

describe("placing events on bars", () => {
  // Five 5-minute bars in broker time, starting 12:00 UTC (15:00 broker).
  const start = utc(2026, 7, 1, 12) + 3 * H;
  const times = [0, 1, 2, 3, 4].map((i) => start + i * 300);
  const all = { impacts: new Set(["high", "medium", "low"]), allCurrencies: true };

  it("puts an event on the bar its broker time falls in", () => {
    const marks = placeNews([ev(utc(2026, 7, 1, 12, 7), "high")], times, 300, 0, all);
    expect(marks.map((m) => m.barTime)).toEqual([times[1]]);
  });

  it("groups a bar's events and takes the strongest impact", () => {
    const at = utc(2026, 7, 1, 12, 10);
    const marks = placeNews(
      [ev(at, "low", "A"), ev(at, "high", "B"), ev(at, "medium", "C")], times, 300, 0, all);
    expect(marks).toHaveLength(1);
    expect(marks[0].impact).toBe("high");
    expect(marks[0].events.map((e) => e.title)).toEqual(["B", "C", "A"]);
  });

  it("leaves out events before the first bar or after the last one closes", () => {
    const marks = placeNews(
      [ev(utc(2026, 7, 1, 11, 59), "high"), ev(utc(2026, 7, 1, 12, 25), "high")],
      times, 300, 0, all);
    expect(marks).toEqual([]);
  });

  it("filters by impact and, unless asked for all, to gold's currencies", () => {
    const at = utc(2026, 7, 1, 12, 0);
    const events = [ev(at, "low", "L"), ev(at, "high", "Euro", "EUR"), ev(at, "medium", "M")];
    const marks = placeNews(events, times, 300, 0,
      { impacts: new Set(["medium", "high"]), allCurrencies: false });
    expect(marks[0].events.map((e) => e.title)).toEqual(["M"]);
  });

  it("applies the measured correction", () => {
    const marks = placeNews([ev(utc(2026, 7, 1, 15, 2), "high")], times, 300, -3 * H, all);
    expect(marks.map((m) => m.barTime)).toEqual([times[0]]);
  });
});

describe("the move after an event", () => {
  const bars = [
    { ts: 0, open: 100, high: 101, low: 99, close: 100 },
    { ts: 300, open: 100, high: 112, low: 98, close: 110 },
    { ts: 600, open: 110, high: 111, low: 104, close: 105 },
  ];

  it("runs from the event bar's open to the close half an hour on", () => {
    // 5-minute bars: six of them, clipped to the bars there are.
    expect(moveAfter(bars, 300, 300)).toEqual({ change: 5, range: 14, bars: 2 });
  });

  it("is one bar on a timeframe longer than half an hour", () => {
    expect(moveAfter(bars, 300, 3600)).toEqual({ change: 10, range: 14, bars: 1 });
  });

  it("is null for a bar that is not loaded", () => {
    expect(moveAfter(bars, 999, 300)).toBeNull();
  });
});

describe("the bar length, when the caller does not give it", () => {
  it("is the smallest step, so a weekend gap does not count", () => {
    expect(barSeconds([0, 300, 600, 200_000, 200_300])).toBe(300);
  });
});
