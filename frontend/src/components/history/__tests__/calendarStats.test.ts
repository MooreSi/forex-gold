import { describe, expect, it } from "vitest";
import type { TradeRow } from "../hooks/useClosedTrades";
import type { DayCell } from "../internal/calendarGrid";
import { dayOpenBalances } from "../internal/calendarGrid";
import {
  cellMetric, dayStats, formatMetric, goalMarks, intradayCurve, streaks, weekdayTotals,
} from "../internal/calendarStats";

/**
 * The Calendar's per-day and per-month statistics (docs/todo/023). Every
 * figure is from the closed-trade rows the calendar already holds, so each
 * test hands in rows and reads a number back.
 */
function row(over: Partial<TradeRow> = {}): TradeRow {
  return { ticket: 1, close_ts: 1, pnl: 0, source: "", direction: "BUY",
           entry_price: 0, exit_price: 0, open_ts: 0, lots: 0, close_lots: [],
           fees: 0, pips: null, duration_secs: null, order_type: "Market",
           pending_secs: null, reason: "", strategy: "", max_tp: "", rr: null,
           spread_points: null, group: null, session: null, ...over } as TradeRow;
}

function cell(date: string, pnls: number[], inMonth = true): DayCell {
  const trades = pnls.map((pnl, i) => row({ ticket: i + 1, pnl, day: date }));
  return { date, day: Number(date.slice(8)), inMonth, trades,
           pnl: pnls.reduce((s, p) => s + p, 0) };
}

describe("dayStats", () => {
  it("works out profit factor, averages, best, worst and fees", () => {
    const s = dayStats([
      row({ pnl: 60, fees: 1 }), row({ pnl: 40, fees: 1.5 }), row({ pnl: -50, fees: 0.5 }),
    ]);
    expect(s.profitFactor).toBeCloseTo(2, 6);
    expect(s.avgWin).toBeCloseTo(50, 6);
    expect(s.avgLoss).toBeCloseTo(-50, 6);
    expect(s.best).toBe(60);
    expect(s.worst).toBe(-50);
    expect(s.fees).toBeCloseTo(3, 6);
  });

  it("gives no profit factor on a day with no losers, not Infinity", () => {
    const s = dayStats([row({ pnl: 60 })]);
    expect(s.profitFactor).toBeNull();
    expect(s.avgLoss).toBeNull();
  });

  it("gives no average win on a day with no winners", () => {
    expect(dayStats([row({ pnl: -5 })]).avgWin).toBeNull();
  });
});

describe("intradayCurve", () => {
  it("runs the day's realised P&L in close order from zero", () => {
    const c = intradayCurve([
      row({ ticket: 3, close_ts: 30, pnl: 10 }),
      row({ ticket: 1, close_ts: 10, pnl: 50 }),
      row({ ticket: 2, close_ts: 20, pnl: -80 }),
    ]);
    expect(c.points.map((p) => p.cum)).toEqual([0, 50, -30, -20]);
    expect(c.peak).toBe(50);
    expect(c.low).toBe(-30);
  });

  it("measures the drawdown from the running peak", () => {
    // Up 50, down to -30: an 80 drawdown, not a 30 loss.
    const c = intradayCurve([
      row({ ticket: 1, close_ts: 10, pnl: 50 }),
      row({ ticket: 2, close_ts: 20, pnl: -80 }),
      row({ ticket: 3, close_ts: 30, pnl: 10 }),
    ]);
    expect(c.maxDrawdown).toBe(80);
  });

  it("gives an all-winning day no drawdown", () => {
    expect(intradayCurve([
      row({ ticket: 1, close_ts: 1, pnl: 5 }), row({ ticket: 2, close_ts: 2, pnl: 5 }),
    ]).maxDrawdown).toBe(0);
  });
});

describe("streaks", () => {
  it("counts green and red runs over traded days in date order", () => {
    const s = streaks([
      cell("2026-09-01", [10]), cell("2026-09-02", [5]), cell("2026-09-03", [-4]),
      cell("2026-09-04", []),   // a quiet day neither breaks nor extends a run
      cell("2026-09-07", [2]), cell("2026-09-08", [3]), cell("2026-09-09", [1]),
    ]);
    expect(s.longestGreen).toBe(3);
    expect(s.longestRed).toBe(1);
    expect(s.current).toEqual({ kind: "green", length: 3 });
    expect(s.greenDays).toBe(5);
    expect(s.redDays).toBe(1);
  });

  it("ignores the padding days from the next and previous months", () => {
    const s = streaks([cell("2026-08-31", [-9], false), cell("2026-09-01", [4])]);
    expect(s.redDays).toBe(0);
    expect(s.current).toEqual({ kind: "green", length: 1 });
  });

  it("has no current streak in a quiet month", () => {
    expect(streaks([cell("2026-09-01", [])]).current).toBeNull();
  });
});

describe("weekdayTotals", () => {
  it("totals the month's in-month days by weekday, Monday first", () => {
    // 2026-09-07 is a Monday, 2026-09-13 a Sunday.
    const t = weekdayTotals([
      cell("2026-09-07", [10, -4]), cell("2026-09-14", [6]),
      cell("2026-09-13", [-3]), cell("2026-08-31", [99], false),
    ]);
    expect(t.map((d) => d.label)).toEqual(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]);
    expect(t[0]).toMatchObject({ pnl: 12, n: 3, wins: 2 });
    expect(t[6]).toMatchObject({ pnl: -3, n: 1, wins: 0 });
  });
});

describe("cellMetric", () => {
  const c: DayCell = { date: "2026-09-15", day: 15, inMonth: true, pnl: 30, trades: [
    row({ ticket: 1, pnl: 50, pips: 12.5, rr: 1.5 }),
    row({ ticket: 2, pnl: -20, pips: -4, rr: null }),
  ] };

  it("gives money, pips and R", () => {
    expect(cellMetric(c, "usd", null)).toBe(30);
    expect(cellMetric(c, "pips", null)).toBeCloseTo(8.5, 6);
    // R sums only the trades that have one.
    expect(cellMetric(c, "r", null)).toBeCloseTo(1.5, 6);
  });

  it("gives the % it is handed", () => {
    expect(cellMetric(c, "pct", 2.5)).toBe(2.5);
    expect(cellMetric(c, "pct", null)).toBeNull();
  });

  it("gives no R for a day where no trade has one", () => {
    expect(cellMetric({ ...c, trades: [row({ rr: null })] }, "r", null)).toBeNull();
  });
});

describe("goalMarks", () => {
  const trades = [
    row({ ticket: 1, close_ts: 1, pnl: 60 }),
    row({ ticket: 2, close_ts: 2, pnl: -30 }),
  ];

  it("marks the goal when the running P&L touched it, even if given back", () => {
    // 1% of 5000 is 50; the day touched 60 and closed at 30.
    const m = goalMarks(trades, 5000, {
      daily_goal_enabled: 1, daily_goal_mode: "pct", daily_goal_value: 1,
    });
    expect(m.goal).toBe(true);
    expect(m.loss).toBe(false);
  });

  it("reads a dollar goal as dollars", () => {
    expect(goalMarks(trades, 5000, {
      daily_goal_enabled: 1, daily_goal_mode: "usd", daily_goal_value: 100,
    }).goal).toBe(false);
  });

  it("marks the loss limit when the running P&L touched it", () => {
    const losing = [row({ ticket: 1, close_ts: 1, pnl: -40 }), row({ ticket: 2, close_ts: 2, pnl: 20 })];
    // 0.5% of 5000 is 25; the day touched -40.
    expect(goalMarks(losing, 5000, { max_daily_loss_pct: 0.5 }).loss).toBe(true);
  });

  it("marks nothing with the goal off and no loss limit", () => {
    expect(goalMarks(trades, 5000, { daily_goal_enabled: 0, max_daily_loss_pct: 0 }))
      .toEqual({ goal: false, loss: false });
  });

  it("marks no percentage goal without an opening balance", () => {
    expect(goalMarks(trades, null, {
      daily_goal_enabled: 1, daily_goal_mode: "pct", daily_goal_value: 1,
    }).goal).toBe(false);
  });
});

describe("dayOpenBalances", () => {
  it("gives each traded day the balance it opened on", () => {
    const open = dayOpenBalances([
      row({ ticket: 2, day: "2026-09-16", pnl: 50 }),
      row({ ticket: 1, day: "2026-09-15", pnl: 50 }),
    ], 1100);
    expect(open.get("2026-09-16")).toBe(1050);
    expect(open.get("2026-09-15")).toBe(1000);
  });
});

describe("formatMetric", () => {
  it("formats each metric for a cell", () => {
    expect(formatMetric("usd", 12_400)).toBe("+$12.4K");
    expect(formatMetric("pct", 1.234)).toBe("+1.23%");
    expect(formatMetric("pips", -8.25)).toBe("-8.3p");
    expect(formatMetric("r", 1.5)).toBe("+1.50R");
  });

  it("shows a missing figure as a dash, not a zero", () => {
    expect(formatMetric("r", null)).toBe("—");
  });
});
