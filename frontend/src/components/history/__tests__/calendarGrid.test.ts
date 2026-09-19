import { describe, expect, it } from "vitest";
import { bySource, groupByDay, monthGrid, tradingDate } from "../internal/calendarGrid";
import type { TradeRow } from "../hooks/useClosedTrades";

/**
 * The Analysis Calendar's arithmetic.
 *
 * Which day a trade belongs to is the whole problem. A close stamp is broker
 * time (UTC+3), so a trade that closed at 01:30 broker time on a Tuesday
 * closed at 22:30 London on the Monday — and putting it on Tuesday moves a
 * loss into the wrong week, the wrong month and the wrong weekly total.
 */
function row(over: Partial<TradeRow> = {}): TradeRow {
  return { ticket: 1, close_ts: 0, pnl: 0, source: "", direction: "BUY",
           entry_price: 0, exit_price: 0, open_ts: 0, lots: 0, close_lots: [],
           fees: 0, pips: null, duration_secs: null, order_type: "Market",
           pending_secs: null, reason: "", strategy: "", max_tp: "", rr: null,
           spread_points: null, group: null, ...over } as TradeRow;
}

/** 2026-09-15 00:30 UTC. Broker time is three hours ahead of UTC. */
const SEP_15_0030_UTC = Date.UTC(2026, 8, 15, 0, 30) / 1000;

describe("which day a trade belongs to", () => {
  it("shifts a close stamp out of broker time", () => {
    // 01:30 broker time on the 15th is 22:30 on the 14th.
    const brokerStamp = Date.UTC(2026, 8, 15, 1, 30) / 1000;

    expect(tradingDate(brokerStamp)).toBe("2026-09-14");
  });

  it("keeps a mid-session trade on its own day", () => {
    expect(tradingDate(Date.UTC(2026, 8, 15, 15, 0) / 1000)).toBe("2026-09-15");
  });

  it("does not depend on the viewer's own timezone", () => {
    // UTC getters after the shift. A local-time getter would apply the
    // machine's offset on top and move the boundary again, differently on
    // every machine that opens the page.
    expect(tradingDate(SEP_15_0030_UTC)).toBe("2026-09-14");
  });
});

describe("grouping", () => {
  it("puts two trades from the same day together", () => {
    const grouped = groupByDay([
      row({ ticket: 1, close_ts: Date.UTC(2026, 8, 15, 12, 0) / 1000 }),
      row({ ticket: 2, close_ts: Date.UTC(2026, 8, 15, 18, 0) / 1000 }),
    ]);

    expect(grouped.get("2026-09-15")).toHaveLength(2);
  });

  it("leaves out a row with no close time rather than putting it in 1970", () => {
    expect(groupByDay([row({ close_ts: 0 })]).size).toBe(0);
  });

  it("survives a missing list", () => {
    expect(groupByDay(undefined as never).size).toBe(0);
  });
});

describe("the month grid", () => {
  const grid = () => monthGrid([
    row({ ticket: 1, close_ts: Date.UTC(2026, 8, 15, 12, 0) / 1000, pnl: 50 }),
    row({ ticket: 2, close_ts: Date.UTC(2026, 8, 15, 18, 0) / 1000, pnl: -20 }),
    row({ ticket: 3, close_ts: Date.UTC(2026, 8, 22, 12, 0) / 1000, pnl: 10 }),
  ], 2026, 9);

  it("covers every day of the month", () => {
    const own = grid().weeks.flatMap((w) => w.days).filter((d) => d.inMonth);

    expect(own).toHaveLength(30);
  });

  it("pads to whole weeks", () => {
    for (const week of grid().weeks) expect(week.days).toHaveLength(7);
  });

  it("starts the week on Monday", () => {
    // A Sunday-first grid splits the weekend across two rows, which puts
    // Friday's close and the Sunday open in different weekly totals.
    // 2026-09-01 is a Tuesday, so Monday 2026-08-31 leads.
    expect(grid().weeks[0]!.days[0]!.date).toBe("2026-08-31");
  });

  it("marks the padding as not belonging to the month", () => {
    expect(grid().weeks[0]!.days[0]!.inMonth).toBe(false);
  });

  it("totals a day's trades", () => {
    const day = grid().weeks.flatMap((w) => w.days).find((d) => d.date === "2026-09-15");

    expect(day!.pnl).toBe(30);
    expect(day!.trades).toHaveLength(2);
  });

  it("totals the month", () => {
    expect(grid().pnl).toBe(40);
    expect(grid().trades).toBe(3);
  });

  it("does not count the padding in a weekly total", () => {
    // Otherwise the same trades are counted in two months.
    const padded = monthGrid(
      [row({ close_ts: Date.UTC(2026, 7, 31, 12, 0) / 1000, pnl: 999 })],
      2026, 9,
    );

    expect(padded.weeks[0]!.pnl).toBe(0);
    expect(padded.pnl).toBe(0);
  });

  it("totals each week separately", () => {
    const weeks = grid().weeks.filter((w) => w.trades > 0);

    expect(weeks.map((w) => w.pnl)).toEqual([30, 10]);
  });

  it("handles a month that starts on a Monday with no leading pad", () => {
    // 2026-06-01 is a Monday.
    const june = monthGrid([], 2026, 6);

    expect(june.weeks[0]!.days[0]!.date).toBe("2026-06-01");
    expect(june.weeks[0]!.days[0]!.inMonth).toBe(true);
  });

  it("pads January from the previous December", () => {
    const jan = monthGrid([], 2026, 1);
    const lead = jan.weeks[0]!.days.filter((d) => !d.inMonth);

    expect(lead.every((d) => d.date.startsWith("2025-12"))).toBe(true);
  });

  it("pads December into the following January", () => {
    const dec = monthGrid([], 2026, 12);
    const trail = dec.weeks.at(-1)!.days.filter((d) => !d.inMonth);

    expect(trail.every((d) => d.date.startsWith("2027-01"))).toBe(true);
  });

  it("gives a quiet month an empty grid rather than no grid", () => {
    const quiet = monthGrid([], 2026, 9);

    expect(quiet.trades).toBe(0);
    expect(quiet.weeks.length).toBeGreaterThan(0);
  });
});

describe("a day's breakdown", () => {
  it("totals each source", () => {
    const rows = [
      row({ source: "GoldSignals", pnl: 50 }),
      row({ source: "GoldSignals", pnl: -20 }),
      row({ source: "NoisyChannel", pnl: -80 }),
    ];

    expect(bySource(rows)).toEqual([
      { source: "NoisyChannel", pnl: -80, n: 1 },
      { source: "GoldSignals", pnl: 30, n: 2 },
    ]);
  });

  it("puts the worst source first, because that is the one to act on", () => {
    const rows = [row({ source: "Good", pnl: 100 }), row({ source: "Bad", pnl: -100 })];

    expect(bySource(rows)[0]!.source).toBe("Bad");
  });

  it("names an unattributed trade rather than dropping it", () => {
    // A trade opened by hand has no source, and leaving it out makes the
    // day's rows disagree with the day's total.
    expect(bySource([row({ source: "", pnl: 5 })])[0]!.source).toBe("unattributed");
  });
});
