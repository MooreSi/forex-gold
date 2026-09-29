/**
 * The Compound Calculator's arithmetic: what a daily goal becomes if it is hit
 * every trading day.
 *
 * A projection, not a record. Nothing here reads a trade, and the section that
 * renders it says so -- a line that only ever goes up is what a plan looks
 * like, not what an account does.
 *
 * Simulated a trading day at a time rather than with a closed-form
 * `(1 + r)^n`, because the two extras break the closed form: profit taken out
 * stops compounding the day it is taken, and a monthly deposit starts
 * compounding the day after it lands.
 *
 * **What a month is.** A year is 52 weeks and 12 months, so a month is 52/12
 * weeks and month `m` ends on trading day `round(m x days-per-week x 52/12)`.
 * Five-day weeks give 22, 43, 65 ... 260: a year of them is exactly 260 days,
 * and the weekly and monthly tables add up to the same final balance.
 */

export const TRADING_WEEKS_PER_MONTH = 52 / 12;

export interface CompoundInputs {
  capital: number;
  /** The goal, as a percent of the balance, each trading day. */
  dailyPct: number;
  daysPerWeek: number;
  months: number;
  /** Share of each day's profit left in the account, 0-100. The rest is taken out. */
  reinvestPct: number;
  /** Paid in at the end of every month. */
  monthlyDeposit: number;
}

export interface Period {
  index: number;
  start: number;
  /** Profit earned in the period, including any of it taken out. */
  gain: number;
  /** `gain` as a percent of `start`. */
  gainPct: number;
  withdrawn: number;
  deposit: number;
  end: number;
}

export interface Projection {
  tradingDays: number;
  /** The trading day each month closes on, 1-based into `balances`. */
  monthEnds: number[];
  /** The balance at the close of each trading day; `[0]` is the capital. */
  balances: number[];
  weeks: Period[];
  months: Period[];
  final: number;
  totalGain: number;
  totalWithdrawn: number;
  totalDeposited: number;
}

function valid(i: CompoundInputs): boolean {
  const finite = [i.capital, i.dailyPct, i.daysPerWeek, i.months,
    i.reinvestPct, i.monthlyDeposit].every(Number.isFinite);
  return finite
    && i.capital > 0
    && i.dailyPct > 0
    && Number.isInteger(i.daysPerWeek) && i.daysPerWeek >= 1 && i.daysPerWeek <= 7
    && Number.isInteger(i.months) && i.months >= 1 && i.months <= 120
    && i.reinvestPct >= 0 && i.reinvestPct <= 100
    && i.monthlyDeposit >= 0;
}

function periods(
  ends: number[], balances: number[], gain: number[], withdrawn: number[], deposit: number[],
): Period[] {
  const out: Period[] = [];
  let from = 0;
  ends.forEach((to, idx) => {
    let g = 0, w = 0, d = 0;
    for (let day = from + 1; day <= to; day++) {
      g += gain[day]; w += withdrawn[day]; d += deposit[day];
    }
    const start = balances[from];
    out.push({
      index: idx + 1, start, gain: g, gainPct: (g / start) * 100,
      withdrawn: w, deposit: d, end: balances[to],
    });
    from = to;
  });
  return out;
}

/** `null` when an input is blank or out of range; the section says which. */
export function project(i: CompoundInputs): Projection | null {
  if (!valid(i)) return null;

  const r = i.dailyPct / 100;
  const keep = i.reinvestPct / 100;
  const monthEnds = Array.from({ length: i.months },
    (_, m) => Math.round((m + 1) * i.daysPerWeek * TRADING_WEEKS_PER_MONTH));
  const days = monthEnds[monthEnds.length - 1];
  const isMonthEnd = new Set(monthEnds);

  const balances = [i.capital];
  const gain = [0], withdrawn = [0], deposit = [0];
  let bal = i.capital;
  for (let day = 1; day <= days; day++) {
    const profit = bal * r;
    bal += profit * keep;
    const paidIn = isMonthEnd.has(day) ? i.monthlyDeposit : 0;
    bal += paidIn;
    gain.push(profit);
    withdrawn.push(profit * (1 - keep));
    deposit.push(paidIn);
    balances.push(bal);
  }

  const weekEnds: number[] = [];
  for (let d = i.daysPerWeek; d < days; d += i.daysPerWeek) weekEnds.push(d);
  weekEnds.push(days);

  const sum = (xs: number[]) => xs.reduce((s, x) => s + x, 0);
  return {
    tradingDays: days,
    monthEnds,
    balances,
    weeks: periods(weekEnds, balances, gain, withdrawn, deposit),
    months: periods(monthEnds, balances, gain, withdrawn, deposit),
    final: bal,
    totalGain: sum(gain),
    totalWithdrawn: sum(withdrawn),
    totalDeposited: sum(deposit),
  };
}

export interface RateEquivalents {
  weeklyPct: number;
  monthlyPct: number;
  yearlyPct: number;
  /** Trading days for the balance to double, everything reinvested. */
  daysToDouble: number;
}

/** What the daily goal amounts to over longer spans, all of it reinvested. */
export function rateEquivalents(dailyPct: number, daysPerWeek: number): RateEquivalents {
  const g = 1 + dailyPct / 100;
  const pct = (days: number) => (g ** days - 1) * 100;
  return {
    weeklyPct: pct(daysPerWeek),
    monthlyPct: pct(daysPerWeek * TRADING_WEEKS_PER_MONTH),
    yearlyPct: pct(daysPerWeek * 52),
    daysToDouble: Math.ceil(Math.log(2) / Math.log(g)),
  };
}
