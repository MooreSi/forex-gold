/**
 * The Set & Forget chart must open on the current market, not on July.
 *
 * Reported on 2026-09-21: "the chart does not appear to be real time, it
 * should show todays chart". The DATA is real time -- `/api/chart/candles`
 * answers with the currently-forming 4H bar and the panel re-polls every 60
 * seconds. What was wrong is the view: 300 bars of 4H is fifty days, drawn
 * all at once, so today is a few pixels at the right-hand edge and the chart
 * looks frozen.
 *
 * The 300 bars stay. They are not decoration: EMA 200 needs them, and asking
 * for fewer makes that line come back all nulls. Only the opening VIEW
 * narrows.
 *
 * The second rule here matters as much: the range is set ONCE. This panel
 * re-polls on a timer, and re-applying the range on every tick would drag the
 * chart back under an operator who had panned it -- the same class of bug as
 * a poll overwriting a field being typed into.
 */
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { visibleRanges } from "@/test/chartStub";
import { SetupChart, VISIBLE_BARS } from "../internal/SetupChart";
import type { Candle } from "@/api/types";

vi.mock("lightweight-charts", async () => {
  const { chartStub: stub } = await import("@/test/chartStub");
  return stub();
});

function candles(n: number): Candle[] {
  const start = 1_789_000_000;
  return Array.from({ length: n }, (_, i) => ({
    ts: start + i * 14_400, open: 4300, high: 4320, low: 4290, close: 4310,
  }));
}

const PROPS = {
  overlays: null, zones: [], fibLevels: [], candidate: null,
  riskMoney: null, rewardMoney: null,
};

beforeEach(() => { visibleRanges.length = 0; });
afterEach(() => vi.restoreAllMocks());

describe("what the chart opens on", () => {
  it("shows the most recent bars, not all three hundred", () => {
    render(<SetupChart candles={candles(300)} {...PROPS} />);

    expect(visibleRanges.length).toBeGreaterThan(0);
    const r = visibleRanges[visibleRanges.length - 1] as { from: number; to: number };
    expect(r.to - r.from).toBeCloseTo(VISIBLE_BARS, 0);
  });

  it("ends at the newest bar, so the live candle is on screen", () => {
    render(<SetupChart candles={candles(300)} {...PROPS} />);

    const r = visibleRanges[visibleRanges.length - 1] as { from: number; to: number };
    expect(r.to).toBeGreaterThanOrEqual(299);
  });

  it("does not narrow a history shorter than the window", () => {
    // A fresh install with 20 bars must not be asked to show 60.
    render(<SetupChart candles={candles(20)} {...PROPS} />);

    const r = visibleRanges[visibleRanges.length - 1] as { from: number; to: number };
    expect(r.from).toBeGreaterThanOrEqual(0);
  });

  it("sets the range once, not on every poll tick", () => {
    // The panel re-polls every 60s. Re-applying would yank the chart back
    // from wherever the operator had panned it.
    const { rerender } = render(<SetupChart candles={candles(300)} {...PROPS} />);
    const afterFirst = visibleRanges.length;

    rerender(<SetupChart candles={candles(301)} {...PROPS} />);

    expect(visibleRanges.length).toBe(afterFirst);
  });

  it("asks for nothing when there are no candles at all", () => {
    render(<SetupChart candles={[]} {...PROPS} />);

    expect(visibleRanges).toEqual([]);
  });
});
