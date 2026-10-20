import { describe, expect, it } from "vitest";
import { bySession, bySource, dayGains, groupByDay, monthGrid, periodGain, tradingDate } from "../internal/calendarGrid";
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

    // `wins` joined the row on 2026-09-22: "$30 from two trades" reads very
    // differently from one winner and one loser, which is what this is. Whole
    // dicts still, so a field that appears without a reason fails here.
    expect(bySource(rows)).toEqual([
      { source: "NoisyChannel", pnl: -80, n: 1, wins: 0 },
      { source: "GoldSignals", pnl: 30, n: 2, wins: 1 },
    ]);
  });

  it("puts the worst source first, because that is the one to act on", () => {
    const rows = [row({ source: "Good", pnl: 100 }), row({ source: "Bad", pnl: -100 })];

    expect(bySource(rows)[0]!.source).toBe("Bad");
  });

  /**
   * "when clicking into each day it should also list the profitability per
   * market as well" — 2026-09-22. One instrument is traded here, so the market
   * that varies is which session was open when the trade closed.
   */
  it("totals each session in the order a day runs through them", () => {
    const rows = [
      row({ session: "ny", pnl: 10 }),
      row({ session: "asian", pnl: -40 }),
      row({ session: "london", pnl: 90 }),
      row({ session: "london", pnl: 10 }),
    ];

    const split = bySession(rows);

    expect(split.rows.map((r) => r.key)).toEqual(["asian", "london", "ny"]);
    expect(split.rows[1]).toMatchObject({ label: "London", pnl: 100, n: 2, wins: 2 });
  });

  it("gives a trade closed 21:00-24:00 UTC its own row, last (bugs/057)", () => {
    // No session since 2026-10-07. Without its own row it would read as
    // "no close stamp", which is a different fact.
    const split = bySession([row({ session: "off", pnl: -15 }), row({ session: "asian", pnl: 4 })]);

    expect(split.rows.map((r) => r.key)).toEqual(["asian", "off"]);
    expect(split.rows[1]).toMatchObject({ label: "Out of session", pnl: -15 });
    expect(split.unplaced).toBe(0);
  });

  it("counts a trade with no session rather than bucketing it", () => {
    // A close with no timestamp carries no session. Filing it under whichever
    // session contains hour zero makes a quiet session look busy.
    const split = bySession([row({ session: null, pnl: 5 }),
                             row({ session: "ny", pnl: 5 })]);

    expect(split.unplaced).toBe(1);
    expect(split.rows).toHaveLength(1);
  });

  it("names an unattributed trade rather than dropping it", () => {
    // A trade opened by hand has no source, and leaving it out makes the
    // day's rows disagree with the day's total.
    expect(bySource([row({ source: "", pnl: 5 })])[0]!.source).toBe("unattributed");
  });
});

/**
 * A day's % gain is of that day's OPENING balance: the balance now, minus
 * everything realised from that day on. The same rule the daily goal uses
 * (`risk/daily_goal.py`), so the calendar's "+1%" and the goal's "1%" are
 * the same one percent.
 */
describe("dayGains", () => {
  it("measures each day against the balance it opened on", () => {
    // Balance now 1100. The 16th made +50, so it opened on 1050; the 15th
    // made +50 before that, so it opened on 1000.
    const gains = dayGains([
      row({ ticket: 2, day: "2026-09-16", close_ts: 2, pnl: 50 }),
      row({ ticket: 1, day: "2026-09-15", close_ts: 1, pnl: 50 }),
    ], 1100);

    expect(gains.get("2026-09-15")).toBeCloseTo(5, 6);
    expect(gains.get("2026-09-16")).toBeCloseTo((50 / 1050) * 100, 6);
  });

  it("adds up every trade on the day, losers included", () => {
    const gains = dayGains([
      row({ ticket: 1, day: "2026-09-15", close_ts: 1, pnl: 80 }),
      row({ ticket: 2, day: "2026-09-15", close_ts: 2, pnl: -30 }),
    ], 1050);

    expect(gains.get("2026-09-15")).toBeCloseTo(5, 6);
  });

  it("gives a losing day a negative gain", () => {
    const gains = dayGains([row({ day: "2026-09-15", close_ts: 1, pnl: -50 })], 950);

    expect(gains.get("2026-09-15")).toBeCloseTo(-5, 6);
  });

  it("gives nothing without a balance to measure against", () => {
    // A percentage of a guessed balance is a wrong number that looks right.
    expect(dayGains([row({ day: "2026-09-15", close_ts: 1, pnl: 50 })], null).size)
      .toBe(0);
  });

  it("gives nothing for a day whose opening balance comes out at or below zero", () => {
    // Only possible when deposits are missing from the arithmetic; a
    // percentage of nothing would be infinite or flip sign.
    expect(dayGains([row({ day: "2026-09-15", close_ts: 1, pnl: 500 })], 400).size)
      .toBe(0);
  });
});

/**
 * A week's or a month's % gain is of the balance it OPENED on, not the sum of
 * its days' percentages: +5% then +5% is 10.25% of the start, and summing
 * day percentages measured against different balances gives neither.
 */
describe("periodGain", () => {
  const rows = [
    row({ ticket: 3, day: "2026-09-22", close_ts: 3, pnl: 40 }),
    row({ ticket: 2, day: "2026-09-16", close_ts: 2, pnl: 52.5 }),
    row({ ticket: 1, day: "2026-09-15", close_ts: 1, pnl: 50 }),
  ];
  // Balance now 1142.5: the 15th opened on 1000, the 16th on 1050.

  it("measures the period against the balance it opened on", () => {
    // 15th +5% of 1000, 16th +5% of 1050: 102.5 on 1000 is 10.25%, not 10%.
    expect(periodGain(rows, 1142.5, "2026-09-14", "2026-09-20")).toBeCloseTo(10.25, 6);
  });

  it("leaves out trades after the period from the gain but not the balance", () => {
    expect(periodGain(rows, 1142.5, "2026-09-21", "2026-09-27"))
      .toBeCloseTo((40 / 1102.5) * 100, 6);
  });

  it("gives null for a period with no trades", () => {
    expect(periodGain(rows, 1142.5, "2026-09-01", "2026-09-13")).toBeNull();
  });

  it("gives null without a balance", () => {
    expect(periodGain(rows, null, "2026-09-14", "2026-09-20")).toBeNull();
  });

  it("gives null when the opening balance comes out at or below zero", () => {
    expect(periodGain(rows, 100, "2026-09-14", "2026-09-20")).toBeNull();
  });
});
