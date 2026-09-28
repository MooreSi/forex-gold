/**
 * The forming candle, moved by each tick between candle refreshes.
 *
 * Asked for on 2026-09-28: "make it update the data in as close to real time
 * as possible". Candles arrive every 10 s; the tick every second. Without this
 * the last bar sits still for up to ten seconds while the bid line moves.
 *
 * MT5 builds its bars from the BID, so the bid is what moves the bar.
 */
import { describe, expect, it } from "vitest";
import { liveBar } from "../internal/liveBar";

const M1 = 60;
const BAR = { time: 1_790_602_260, open: 4155.31, high: 4155.52, low: 4154.63, close: 4155.26 };

describe("liveBar", () => {
  it("moves the close of the bar the tick falls in", () => {
    expect(liveBar(BAR, { bid: 4155.4, timestamp: BAR.time + 25 }, M1)).toEqual({
      ...BAR, close: 4155.4,
    });
  });

  it("raises the high when the bid trades above it", () => {
    const next = liveBar(BAR, { bid: 4156, timestamp: BAR.time + 25 }, M1);
    expect(next?.high).toBe(4156);
    expect(next?.low).toBe(BAR.low);
  });

  it("lowers the low when the bid trades below it", () => {
    const next = liveBar(BAR, { bid: 4154, timestamp: BAR.time + 25 }, M1);
    expect(next?.low).toBe(4154);
    expect(next?.high).toBe(BAR.high);
  });

  it("opens a new bar when the tick is past the end of the last one", () => {
    expect(liveBar(BAR, { bid: 4155, timestamp: BAR.time + 61 }, M1)).toEqual({
      time: BAR.time + 60, open: 4155, high: 4155, low: 4155, close: 4155,
    });
  });

  it("puts the new bar on the timeframe's boundary, not at the tick's second", () => {
    const m5 = { ...BAR, time: 1_790_602_200 }; // a real 5m boundary
    const next = liveBar(m5, { bid: 4155, timestamp: m5.time + 5 * 60 + 17 }, 5 * M1);
    expect(next?.time).toBe(m5.time + 5 * 60);
  });

  it("ignores a tick older than the bar on the chart", () => {
    // lightweight-charts throws on an update earlier than its last bar.
    expect(liveBar(BAR, { bid: 4155, timestamp: BAR.time - 1 }, M1)).toBeNull();
  });

  it("ignores a tick with no usable price or time", () => {
    expect(liveBar(BAR, { bid: Number.NaN, timestamp: BAR.time + 5 }, M1)).toBeNull();
    expect(liveBar(BAR, { bid: 4155, timestamp: 0 }, M1)).toBeNull();
  });
});
