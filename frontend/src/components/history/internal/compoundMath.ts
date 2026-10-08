/**
 * The Compound Calculator's arithmetic: what a daily goal becomes if it is hit
 * on every winning day, and the max daily drawdown on every losing one.
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
 *
 * **Losing days** (owner, 2026-10-08) take the place of winning days, at the
 * END of each week, and lose the full max drawdown as a percent of that day's
 * balance -- so the dollar loss grows with the account, as the profit does.
 * End of week is the cautious placement: the balance is at its highest, so
 * each loss is at its largest. With everything reinvested the order does not
 * change the final balance; it does change the months, which split weeks. A
 * loss comes out of the account in full: "profit reinvested" is a share of
 * PROFIT, and a losing day has none to take out.
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
  /** Lost on each losing day, as a percent of that day's balance. */
  drawdownPct: number;
  /** How many of each week's trading days lose `drawdownPct`, 0 to `daysPerWeek`. */
  losingDays: number;
}

export interface Period {
  index: number;
  start: number;
  /** Profit earned on the period's winning days, including any of it taken out. */
  gain: number;
  /** `gain` as a percent of `start`. */
  gainPct: number;
  /** Lost on the period's losing days, as a positive number. */
  loss: number;
  /** `gain - loss`. */
  net: number;
  /** `net` as a percent of `start`. */
  netPct: number;
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
  totalLoss: number;
  totalWithdrawn: number;
  totalDeposited: number;
}

function valid(i: CompoundInputs): boolean {
  const finite = [i.capital, i.dailyPct, i.daysPerWeek, i.months,
    i.reinvestPct, i.monthlyDeposit, i.drawdownPct, i.losingDays].every(Number.isFinite);
  return finite
    && i.capital > 0
    && i.dailyPct > 0
    && Number.isInteger(i.daysPerWeek) && i.daysPerWeek >= 1 && i.daysPerWeek <= 7
    && Number.isInteger(i.months) && i.months >= 1 && i.months <= 120
    && i.reinvestPct >= 0 && i.reinvestPct <= 100
    && i.monthlyDeposit >= 0
    && i.drawdownPct >= 0 && i.drawdownPct < 100
    && Number.isInteger(i.losingDays) && i.losingDays >= 0 && i.losingDays <= i.daysPerWeek;
}

function periods(
  ends: number[], balances: number[], gain: number[], loss: number[],
  withdrawn: number[], deposit: number[],
): Period[] {
  const out: Period[] = [];
  let from = 0;
  ends.forEach((to, idx) => {
    let g = 0, l = 0, w = 0, d = 0;
    for (let day = from + 1; day <= to; day++) {
      g += gain[day]; l += loss[day]; w += withdrawn[day]; d += deposit[day];
    }
    const start = balances[from];
    out.push({
      index: idx + 1, start, gain: g, gainPct: (g / start) * 100,
      loss: l, net: g - l, netPct: ((g - l) / start) * 100,
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
  const dd = i.drawdownPct / 100;
  const keep = i.reinvestPct / 100;
  const firstLosing = i.daysPerWeek - i.losingDays + 1;
  const monthEnds = Array.from({ length: i.months },
    (_, m) => Math.round((m + 1) * i.daysPerWeek * TRADING_WEEKS_PER_MONTH));
  const days = monthEnds[monthEnds.length - 1];
  const isMonthEnd = new Set(monthEnds);

  const balances = [i.capital];
  const gain = [0], loss = [0], withdrawn = [0], deposit = [0];
  let bal = i.capital;
  for (let day = 1; day <= days; day++) {
    const losing = ((day - 1) % i.daysPerWeek) + 1 >= firstLosing;
    const profit = losing ? 0 : bal * r;
    const lost = losing ? bal * dd : 0;
    bal += profit * keep - lost;
    const paidIn = isMonthEnd.has(day) ? i.monthlyDeposit : 0;
    bal += paidIn;
    gain.push(profit);
    loss.push(lost);
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
    weeks: periods(weekEnds, balances, gain, loss, withdrawn, deposit),
    months: periods(monthEnds, balances, gain, loss, withdrawn, deposit),
    final: bal,
    totalGain: sum(gain),
    totalLoss: sum(loss),
    totalWithdrawn: sum(withdrawn),
    totalDeposited: sum(deposit),
  };
}

export interface RateEquivalents {
  weeklyPct: number;
  monthlyPct: number;
  yearlyPct: number;
  /**
   * Trading days until the balance is double and stays there, everything
   * reinvested. `null` when a week loses money, so it never does.
   */
  daysToDouble: number | null;
}

/** Past this many weeks "doubles in" is not a plan; it is reported as never. */
const DOUBLING_WEEKS_CAP = 10_000;

/**
 * What the daily goal, net of the losing days, amounts to over longer spans,
 * all of it reinvested.
 *
 * Doubling is counted from the day the balance reaches 2x AND does not fall
 * back. With losses at the end of the week a balance can touch 2x on
 * Wednesday and be under it by Friday; the first touch would flatter the
 * plan. A growing week's lowest point after any day is its own close, and
 * every later week closes higher, so: find the first week that closes at 2x,
 * then the first day in it that is.
 */
export function rateEquivalents(
  dailyPct: number, daysPerWeek: number, drawdownPct = 0, losingDays = 0,
): RateEquivalents {
  const g = 1 + dailyPct / 100;
  const l = 1 - drawdownPct / 100;
  const week = g ** (daysPerWeek - losingDays) * l ** losingDays;
  const pct = (weeks: number) => (week ** weeks - 1) * 100;

  let daysToDouble: number | null = null;
  if (week > 1) {
    let start = 1;
    for (let w = 0; w < DOUBLING_WEEKS_CAP && daysToDouble === null; w++) {
      if (start * week >= 2) {
        let bal = start;
        for (let d = 1; d <= daysPerWeek; d++) {
          bal *= d <= daysPerWeek - losingDays ? g : l;
          if (bal >= 2) { daysToDouble = w * daysPerWeek + d; break; }
        }
      }
      start *= week;
    }
  }
  return {
    weeklyPct: pct(1),
    monthlyPct: pct(TRADING_WEEKS_PER_MONTH),
    yearlyPct: pct(52),
    daysToDouble,
  };
}

export interface BreakEven {
  /** The fewest winning days in a week of `daysPerWeek` that do not lose money. */
  minWinningDays: number;
  /** The daily goal that exactly offsets the losing days given; `null` if every day loses. */
  minDailyPct: number | null;
}

/** Where the plan stops losing money, two ways: more winning days, or a bigger goal. */
export function breakEven(
  dailyPct: number, daysPerWeek: number, drawdownPct: number, losingDays: number,
): BreakEven {
  const g = 1 + dailyPct / 100;
  const l = 1 - drawdownPct / 100;
  let minWinningDays = daysPerWeek;
  for (let w = 0; w <= daysPerWeek; w++) {
    if (g ** w * l ** (daysPerWeek - w) >= 1) { minWinningDays = w; break; }
  }
  const winning = daysPerWeek - losingDays;
  return {
    minWinningDays,
    minDailyPct: winning > 0 ? (l ** (-losingDays / winning) - 1) * 100 : null,
  };
}
