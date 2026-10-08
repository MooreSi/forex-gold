import { describe, expect, it } from "vitest";
import {
  breakEven, project, rateEquivalents, TRADING_WEEKS_PER_MONTH, type CompoundInputs,
} from "../internal/compoundMath";

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
    reinvestPct: 100, monthlyDeposit: 0, drawdownPct: 2, losingDays: 0, ...over,
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
    ["more losing days than trading days", { daysPerWeek: 3, losingDays: 4 }],
    ["negative losing days", { losingDays: -1 }],
    ["part of a losing day", { losingDays: 1.5 }],
    ["a negative drawdown", { drawdownPct: -1 }],
    ["a drawdown of the whole balance", { drawdownPct: 100 }],
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

describe("losing days", () => {
  // 1% goal, 2 of 5 days lose the 2% max drawdown.
  const losing = { losingDays: 2, drawdownPct: 2 };

  it("replaces winning days rather than adding to them", () => {
    const p = project(inputs(losing))!;

    expect(p.weeks[0].end).toBeCloseTo(1000 * 1.01 ** 3 * 0.98 ** 2, 6);
    expect(p.tradingDays).toBe(22);
  });

  it("falls at the end of each week, when the balance is highest", () => {
    const p = project(inputs(losing))!;

    expect(p.balances[3]).toBeCloseTo(1000 * 1.01 ** 3, 6);
    expect(p.balances[4]).toBeCloseTo(1000 * 1.01 ** 3 * 0.98, 6);
    expect(p.balances[6]).toBeCloseTo(1000 * 1.01 ** 4 * 0.98 ** 2, 6);
  });

  it("loses a percent of that day's balance, so the dollar loss moves with it", () => {
    const p = project(inputs(losing))!;
    const w1 = p.weeks[0], w2 = p.weeks[1];

    expect(w1.loss).toBeCloseTo(1000 * 1.01 ** 3 * 0.02 + 1000 * 1.01 ** 3 * 0.98 * 0.02, 6);
    expect(w2.loss).toBeCloseTo(w1.loss * (w2.start / w1.start), 6);
  });

  it("keeps profit and loss apart, and nets them", () => {
    const p = project(inputs(losing))!;
    const w = p.weeks[0];

    expect(w.gain).toBeCloseTo(1000 * 1.01 ** 3 - 1000, 6);
    expect(w.net).toBeCloseTo(w.gain - w.loss, 9);
    expect(w.net).toBeCloseTo(w.end - w.start, 6);
    expect(w.netPct).toBeCloseTo((w.net / w.start) * 100, 9);
  });

  it("takes the whole loss out of the balance, whatever share of profit is kept", () => {
    // Nothing reinvested: four winning days are all taken out, and the
    // losing day still comes off the account.
    const p = project(inputs({ losingDays: 1, drawdownPct: 1, reinvestPct: 0 }))!;

    expect(p.balances[4]).toBe(1000);
    expect(p.balances[5]).toBeCloseTo(990, 9);
    expect(p.weeks[0].withdrawn).toBeCloseTo(40, 9);
  });

  it("adds up: capital, plus profit, less loss, less taken out, plus paid in", () => {
    const p = project(inputs({ ...losing, months: 7, reinvestPct: 60, monthlyDeposit: 300 }))!;

    expect(p.final).toBeCloseTo(
      1000 + p.totalGain - p.totalLoss - p.totalWithdrawn + p.totalDeposited, 6);
    expect(p.months.reduce((s, m) => s + m.loss, 0)).toBeCloseTo(p.totalLoss, 6);
    expect(p.weeks.reduce((s, w) => s + w.loss, 0)).toBeCloseTo(p.totalLoss, 6);
  });

  it("loses nothing with no losing days, whatever the drawdown", () => {
    const p = project(inputs({ drawdownPct: 20 }))!;

    expect(p.totalLoss).toBe(0);
    expect(p.final).toBeCloseTo(1000 * 1.01 ** 22, 6);
  });
});

describe("the rate equivalents, with losing days", () => {
  it("nets the losing days into the weekly, monthly and yearly rates", () => {
    const r = rateEquivalents(1, 5, 2, 2);
    const week = 1.01 ** 3 * 0.98 ** 2;

    expect(r.weeklyPct).toBeCloseTo((week - 1) * 100, 6);
    expect(r.monthlyPct).toBeCloseTo((week ** (52 / 12) - 1) * 100, 6);
    expect(r.yearlyPct).toBeCloseTo((week ** 52 - 1) * 100, 6);
  });

  it("never doubles a plan that loses money each week", () => {
    expect(rateEquivalents(1, 5, 2, 2).daysToDouble).toBeNull();
  });

  it("counts the day the balance doubles for good, not the first day it touches double", () => {
    // 2% goal, one 4% losing day in five. The balance first reaches 2x on day
    // 84 and falls back under it at the end of that week and again on day 90;
    // from day 91 it never does.
    expect(rateEquivalents(2, 5, 4, 1).daysToDouble).toBe(91);
  });
});

describe("the break-even point", () => {
  it("names the fewest winning days a week that do not lose money", () => {
    // 1.01^3 x 0.98^2 = 0.990 loses; 1.01^4 x 0.98 = 1.020 does not.
    expect(breakEven(1, 5, 2, 2).minWinningDays).toBe(4);
  });

  it("names the daily goal that breaks even on the losing days given", () => {
    const b = breakEven(1, 5, 2, 2);

    expect(b.minDailyPct).toBeCloseTo((0.98 ** (-2 / 3) - 1) * 100, 9);
    expect((1 + b.minDailyPct! / 100) ** 3 * 0.98 ** 2).toBeCloseTo(1, 9);
  });

  it("has no break-even goal when every trading day loses", () => {
    expect(breakEven(1, 5, 2, 5).minDailyPct).toBeNull();
  });
});
