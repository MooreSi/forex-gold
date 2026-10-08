import type { ReactNode } from "react";
import { StatCard } from "@/components/shared/StatCard";
import { cn } from "@/lib/cn";
import { formatCompactMoney, formatMoney, formatPercent } from "@/components/shared/format";
import { useCompoundCalculator } from "../hooks/useCompoundCalculator";
import { CompoundBalanceSection } from "./CompoundBalanceSection";
import { CompoundBreakdownSection } from "./CompoundBreakdownSection";
import { CompoundInputsForm } from "./CompoundInputsForm";
import { CompoundProfitBarsSection } from "./CompoundProfitBarsSection";

/**
 * The Compound Calculator: what a daily goal becomes if it is hit every
 * winning day, net of a max drawdown on the losing ones.
 *
 * **A projection, not a record, and it says so.** Nothing here reads a trade
 * or talks to the server -- the owner asked for a planning tool beside the
 * analysis (2026-09-29). A curve that only ever goes up is the best case by
 * construction, so two things sit beside it on purpose: the same plan at
 * half the goal, and a sentence naming what the numbers leave out. Losing
 * days (owner, 2026-10-08) bend it down, and when they are set the
 * break-even line says how far the plan is from losing money.
 */
const money = (v: number) => (Math.abs(v) >= 1e9 ? formatCompactMoney(v) : formatMoney(v));

/** A monthly rate past this is flagged as a ceiling rather than a plan. */
const AMBITIOUS_MONTHLY_PCT = 20;

function Figure({ testId, children }: { testId: string; children: ReactNode }) {
  return <span data-testid={testId}>{children}</span>;
}

export function CompoundCalculatorSection({ balance }: { balance: number | null }) {
  const c = useCompoundCalculator(balance);
  const p = c.projection;
  const multiple = p ? p.final / c.inputs.capital : 0;
  const losing = c.inputs.losingDays > 0;

  return (
    <div className="space-y-4">
      <CompoundInputsForm c={c} balance={balance} />

      {c.error || !p || !c.half || !c.rates ? (
        <p role="alert" className="rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
          {c.error ?? "Those numbers cannot be projected."}
        </p>
      ) : (
        <>
          <div className="grid gap-2 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,2fr)]">
            <div className="rounded border border-accent/40 bg-gradient-to-br from-accent/15 to-transparent px-4 py-3">
              <p className="text-[10px] uppercase tracking-wide text-ink-3">
                Balance after {c.inputs.months} {c.inputs.months === 1 ? "month" : "months"}
              </p>
              <p className="num mt-1 text-2xl font-bold text-accent">
                <Figure testId="cc-final">{money(p.final)}</Figure>
              </p>
              <p className="num mt-1 text-[11px] text-ink-2">
                {multiple.toLocaleString("en-GB", { maximumFractionDigits: 1 })}x the start
                {" "}· {p.tradingDays} trading days
              </p>
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              <StatCard label={losing ? "Net profit" : "Total profit"}
                valueClassName={p.totalGain - p.totalLoss < 0 ? "text-loss" : "text-profit"}
                value={<Figure testId="cc-profit">{money(p.totalGain - p.totalLoss)}</Figure>}
                hint="Everything the trading earned, less the losing days, including any of it taken out." />
              {losing && (
                <StatCard label="Lost on losing days" valueClassName="text-loss"
                  value={<Figure testId="cc-loss">{money(p.totalLoss)}</Figure>}
                  hint={`${c.inputs.losingDays} ${c.inputs.losingDays === 1 ? "day" : "days"} a week at the full max daily drawdown, over the whole horizon.`} />
              )}
              <StatCard label="First day's target"
                value={<Figure testId="cc-day-one">{formatMoney(c.inputs.capital * c.inputs.dailyPct / 100)}</Figure>}
                hint="The goal in dollars on day one. It grows with the balance." />
              <StatCard label="At half the goal" valueClassName="text-series-3"
                value={<Figure testId="cc-half">{money(c.half.final)}</Figure>}
                hint="The same months at half the daily goal, with the same losing days: the dashed line on the chart." />
              <StatCard label="Weekly / monthly"
                value={`${formatPercent(c.rates.weeklyPct, 1)} / ${formatPercent(c.rates.monthlyPct, 1)}`}
                hint={losing
                  ? "The daily goal, net of the losing days, compounded over a week and over a month."
                  : "The daily goal compounded over a week and over a month."} />
              <StatCard label="Yearly equivalent"
                value={c.rates.yearlyPct >= 1e5
                  ? `${Math.round(c.rates.yearlyPct / 100).toLocaleString("en-GB")}x`
                  : formatPercent(c.rates.yearlyPct, 0)}
                hint="The daily goal compounded over 52 weeks, all of it reinvested." />
              <StatCard label="Doubles in"
                valueClassName={c.rates.daysToDouble === null ? "text-loss" : undefined}
                value={c.rates.daysToDouble === null ? "Never" : `${c.rates.daysToDouble} days`}
                hint={c.rates.daysToDouble === null
                  ? "Each week loses more on its losing days than it makes on its winning ones."
                  : `About ${Math.ceil(c.rates.daysToDouble / c.inputs.daysPerWeek)} weeks of trading, all of it reinvested, counted from the day it stays doubled.`} />
              {p.totalDeposited > 0 && (
                <StatCard label="Paid in"
                  value={<Figure testId="cc-deposited">{money(p.totalDeposited)}</Figure>} />
              )}
              {p.totalWithdrawn > 0 && (
                <StatCard label="Taken out" valueClassName="text-warning"
                  value={<Figure testId="cc-withdrawn">{money(p.totalWithdrawn)}</Figure>}
                  hint="Profit not reinvested, over the whole horizon." />
              )}
            </div>
          </div>

          {c.rates.monthlyPct > AMBITIOUS_MONTHLY_PCT && (
            <p className="rounded border border-warning/40 bg-warning/10 px-3 py-2 text-[11px] text-warning">
              {formatPercent(c.inputs.dailyPct, 2)} a day is {formatPercent(c.rates.monthlyPct, 0)} a
              month. Held for a year that is {formatPercent(c.rates.yearlyPct, 0)}: treat it as a
              ceiling, not a forecast.
            </p>
          )}

          {c.even && (
            <p data-testid="cc-break-even" className={cn(
              "rounded border px-3 py-2 text-[11px]",
              c.rates.weeklyPct < 0
                ? "border-loss/40 bg-loss/10 text-loss"
                : "border-line bg-surface-2 text-ink-2",
            )}>
              {c.rates.weeklyPct < 0 ? "This plan loses money. " : ""}
              At a {formatPercent(c.inputs.drawdownPct, 2)} max daily drawdown, breaking even takes
              {" "}{c.even.minWinningDays} winning {c.even.minWinningDays === 1 ? "day" : "days"} a
              week out of {c.inputs.daysPerWeek} (this plan has {c.inputs.daysPerWeek - c.inputs.losingDays})
              {c.even.minDailyPct === null
                ? ". With every trading day a losing one, no daily goal breaks even."
                : <>, or a daily goal of at least {formatPercent(c.even.minDailyPct, 2)} with{" "}
                  {c.inputs.losingDays} losing {c.inputs.losingDays === 1 ? "day" : "days"}.</>}
            </p>
          )}

          <CompoundBalanceSection
            projection={p}
            half={c.half}
            monthlyDeposit={c.inputs.monthlyDeposit}
            daysPerWeek={c.inputs.daysPerWeek}
            logScale={c.logScale}
            onLogScale={c.setLogScale}
          />

          <div data-testid="cc-bars" className="grid gap-3 md:grid-cols-2">
            <CompoundProfitBarsSection periods={p.months} noun="Month" net={losing} />
            <CompoundProfitBarsSection periods={p.weeks} noun="Week" net={losing} />
          </div>

          <CompoundBreakdownSection projection={p} capital={c.inputs.capital}
            view={c.view} onView={c.setView} />

          <p className="text-[11px] text-ink-3">
            A projection, not a forecast. {losing
              ? "It assumes the goal is hit on every winning day and the full max drawdown on every losing day, with the losing days at the end of each week,"
              : "It assumes the goal is hit every trading day, with no losing days,"}
            {" "}and leaves out spread, commission, swap and tax. A month is 52/12 weeks, so a year
            of five-day weeks is 260 trading days.
          </p>
        </>
      )}
    </div>
  );
}
