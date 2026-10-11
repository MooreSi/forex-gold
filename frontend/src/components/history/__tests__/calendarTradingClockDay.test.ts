import { describe, expect, it } from "vitest";
import { groupByDay, monthGrid } from "../internal/calendarGrid";
import type { TradeRow } from "../hooks/useClosedTrades";

/**
 * The Calendar files a close on the trading clock's day (owner, 2026-10-10:
 * every "day" in the app is the Trading Clock's, so the Calendar, the header's
 * Today's Goal, the goal itself and the Dashboard's Today agree).
 *
 * The browser does not know the clock's offset, so the backend sends each
 * row's `day` and the Calendar uses it. Without one (an older backend) the
 * UTC date is the fallback, as before.
 */
function row(over: Partial<TradeRow> = {}): TradeRow {
  return { ticket: 1, close_ts: 0, pnl: 0, source: "", direction: "BUY",
           entry_price: 0, exit_price: 0, open_ts: 0, lots: 0, close_lots: [],
           fees: 0, pips: null, duration_secs: null, order_type: "Market",
           pending_secs: null, reason: "", strategy: "", max_tp: "", rr: null,
           spread_points: null, group: null, session: null, ...over } as TradeRow;
}

/** 00:30 BST on 1 October is 23:30 UTC on 30 September; broker time +3h. */
const BST_0030_OCT_1 = Date.UTC(2026, 8, 30, 23, 30) / 1000 + 3 * 3600;

describe("the day a close is filed under", () => {
  it("is the backend's trading-clock day when the row carries one", () => {
    const rows = [row({ close_ts: BST_0030_OCT_1, pnl: 9, day: "2026-10-01" })];

    expect([...groupByDay(rows).keys()]).toEqual(["2026-10-01"]);
    const october = monthGrid(rows, 2026, 10);
    expect(october.pnl).toBe(9);
    expect(monthGrid(rows, 2026, 9).pnl).toBe(0);
  });

  it("falls back to the UTC date for a row without one", () => {
    const rows = [row({ close_ts: BST_0030_OCT_1, pnl: 9 })];

    expect([...groupByDay(rows).keys()]).toEqual(["2026-09-30"]);
  });
});
