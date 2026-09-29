import { describe, expect, it } from "vitest";
import { project, rateEquivalents, TRADING_WEEKS_PER_MONTH, type CompoundInputs } from "../internal/compoundMath";

/**
 * The Compound Calculator's arithmetic.
 *
 * It is a projection, not a record: nothing here reads a trade. What has to
 * be right is the bookkeeping -- a week that starts where the last one ended,
 * a month's gain that excludes the money paid in, profit taken out that stops
 * compounding -- because a projection that double-counts a deposit as profit
 * tells the owner the plan is working better than the plan does.
 */
function inputs(over: Partial<CompoundInputs> = {}): CompoundInputs {
  return {
    capital: 1000, dailyPct: 1, daysPerWeek: 5, months: 1,
    reinvestPct: 100, monthlyDeposit: 0, ...over,
  };
}

describe("the weeks", () => {
  it("compounds the daily goal across each trading day of the week", () => {
    const p = project(inputs())!;

    expect(p.weeks[0].start).toBe(1000);
    expect(p.weeks[0].end).toBeCloseTo(1000 * 1.01 ** 5, 6);
    expect(p.weeks[0].gain).toBeCloseTo(1000 * 1.01 ** 5 - 1000, 6);
    expect(p.weeks[0].gainPct).toBeCloseTo((1.01 ** 5 - 1) * 100, 6);
  });

  it("starts each week where the previous one ended", () => {
    const p = project(inputs({ months: 3 }))!;

    for (let i = 1; i < p.weeks.length; i++) {
      expect(p.weeks[i].start).toBeCloseTo(p.weeks[i - 1].end, 9);
    }
  });

  it("uses the days per week it is given, not five", () => {
    const p = project(inputs({ daysPerWeek: 3 }))!;

    expect(p.weeks[0].end).toBeCloseTo(1000 * 1.01 ** 3, 6);
  });
});

describe("the months", () => {
  it("counts a year of five-day weeks as 260 trading days", () => {
    const p = project(inputs({ months: 12 }))!;

    expect(p.tradingDays).toBe(260);
    expect(p.months).toHaveLength(12);
    expect(p.final).toBeCloseTo(1000 * 1.01 ** 260, 4);
  });

  it("gives a month its share of the year's trading days", () => {
    // 5 days x 52/12 weeks = 21.67, so the first month ends on day 22.
    const p = project(inputs())!;

    expect(TRADING_WEEKS_PER_MONTH).toBeCloseTo(52 / 12, 9);
    expect(p.months[0].end).toBeCloseTo(1000 * 1.01 ** 22, 6);
  });

  it("adds the monthly gains up to the whole gain", () => {
    const p = project(inputs({ months: 6 }))!;

    const summed = p.months.reduce((s, m) => s + m.gain, 0);
    expect(summed).toBeCloseTo(p.totalGain, 6);
    expect(p.totalGain).toBeCloseTo(p.final - 1000, 6);
  });
});

describe("money paid in", () => {
  it("is added at the month's end and is not counted as gain", () => {
    const p = project(inputs({ monthlyDeposit: 500 }))!;
    const m = p.months[0];

    expect(m.deposit).toBe(500);
    expect(m.gain).toBeCloseTo(1000 * 1.01 ** 22 - 1000, 6);
    expect(m.end).toBeCloseTo(1000 * 1.01 ** 22 + 500, 6);
    expect(p.totalDeposited).toBe(500);
  });

  it("compounds from the next month on", () => {
    const p = project(inputs({ months: 2, monthlyDeposit: 500 }))!;

    expect(p.months[1].start).toBeCloseTo(p.months[0].end, 9);
    expect(p.months[1].gainPct).toBeCloseTo(
      (p.months[1].gain / p.months[1].start) * 100, 9);
  });
});

describe("profit taken out", () => {
  it("leaves the balance flat when none is reinvested", () => {
    const p = project(inputs({ reinvestPct: 0 }))!;

    expect(p.final).toBe(1000);
    expect(p.totalWithdrawn).toBeCloseTo(10 * 22, 6);
    expect(p.months[0].withdrawn).toBeCloseTo(220, 6);
  });

  it("still counts withdrawn profit as the month's gain", () => {
    // The trading earned it; only the balance did not keep it.
    const p = project(inputs({ reinvestPct: 0 }))!;

    expect(p.months[0].gain).toBeCloseTo(220, 6);
    expect(p.months[0].gainPct).toBeCloseTo(22, 6);
  });

  it("splits a day's profit between the balance and the pocket", () => {
    const p = project(inputs({ reinvestPct: 50 }))!;

    expect(p.weeks[0].end).toBeCloseTo(1000 * 1.005 ** 5, 6);
  });
});

describe("the daily balances", () => {
  it("starts at the capital and has one point per trading day", () => {
    const p = project(inputs())!;

    expect(p.balances[0]).toBe(1000);
    expect(p.balances).toHaveLength(p.tradingDays + 1);
  });
});

describe("what it refuses", () => {
  it.each([
    ["no capital", { capital: 0 }],
    ["a negative goal", { dailyPct: -1 }],
    ["a zero goal", { dailyPct: 0 }],
    ["an eight-day week", { daysPerWeek: 8 }],
    ["a zero-day week", { daysPerWeek: 0 }],
    ["no horizon", { months: 0 }],
    ["reinvesting more than all of it", { reinvestPct: 101 }],
    ["a negative deposit", { monthlyDeposit: -5 }],
    ["a blank box", { capital: NaN }],
  ])("returns nothing for %s", (_name, over) => {
    expect(project(inputs(over))).toBeNull();
  });
});

describe("the rate equivalents", () => {
  it("turns a daily rate into weekly, monthly and yearly ones", () => {
    const r = rateEquivalents(1, 5);

    expect(r.weeklyPct).toBeCloseTo((1.01 ** 5 - 1) * 100, 6);
    expect(r.monthlyPct).toBeCloseTo((1.01 ** (5 * 52 / 12) - 1) * 100, 6);
    expect(r.yearlyPct).toBeCloseTo((1.01 ** 260 - 1) * 100, 6);
  });

  it("says how many trading days it takes to double", () => {
    // ln 2 / ln 1.01 = 69.66, and a part-day does not double anything.
    expect(rateEquivalents(1, 5).daysToDouble).toBe(70);
    expect(rateEquivalents(2, 5).daysToDouble).toBe(36);
  });
});
