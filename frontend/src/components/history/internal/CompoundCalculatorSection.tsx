import type { ReactNode } from "react";
import { StatCard } from "@/components/shared/StatCard";
import { formatCompactMoney, formatMoney, formatPercent } from "@/components/shared/format";
import { useCompoundCalculator } from "../hooks/useCompoundCalculator";
import { CompoundBalanceSection } from "./CompoundBalanceSection";
import { CompoundBreakdownSection } from "./CompoundBreakdownSection";
import { CompoundInputsForm } from "./CompoundInputsForm";

/**
 * The Compound Calculator: what a daily goal becomes if it is hit every
 * trading day.
 *
 * **A projection, not a record, and it says so.** Nothing here reads a trade
 * or talks to the server -- the owner asked for a planning tool beside the
 * analysis (2026-09-29). A curve that only ever goes up is the best case by
 * construction, so two things sit beside it on purpose: the same plan at
 * half the goal, and a sentence naming what the numbers leave out.
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
              <StatCard label="Total profit" valueClassName="text-profit"
                value={<Figure testId="cc-profit">{money(p.totalGain)}</Figure>}
                hint="Everything the trading earned, including any of it taken out." />
              <StatCard label="First day's target"
                value={<Figure testId="cc-day-one">{formatMoney(c.inputs.capital * c.inputs.dailyPct / 100)}</Figure>}
                hint="The goal in dollars on day one. It grows with the balance." />
              <StatCard label="At half the goal" valueClassName="text-series-3"
                value={<Figure testId="cc-half">{money(c.half.final)}</Figure>}
                hint="The same months at half the daily goal: the dashed line on the chart." />
              <StatCard label="Weekly / monthly"
                value={`${formatPercent(c.rates.weeklyPct, 1)} / ${formatPercent(c.rates.monthlyPct, 1)}`}
                hint="The daily goal compounded over a week and over a month." />
              <StatCard label="Yearly equivalent"
                value={c.rates.yearlyPct >= 1e5
                  ? `${Math.round(c.rates.yearlyPct / 100).toLocaleString("en-GB")}x`
                  : formatPercent(c.rates.yearlyPct, 0)}
                hint="The daily goal compounded over 52 weeks, all of it reinvested." />
              <StatCard label="Doubles in"
                value={`${c.rates.daysToDouble} days`}
                hint={`About ${Math.ceil(c.rates.daysToDouble / c.inputs.daysPerWeek)} weeks of trading, all of it reinvested.`} />
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

          <CompoundBalanceSection
            projection={p}
            half={c.half}
            monthlyDeposit={c.inputs.monthlyDeposit}
            daysPerWeek={c.inputs.daysPerWeek}
            logScale={c.logScale}
            onLogScale={c.setLogScale}
          />

          <CompoundBreakdownSection projection={p} capital={c.inputs.capital}
            view={c.view} onView={c.setView} />

          <p className="text-[11px] text-ink-3">
            A projection, not a forecast. It assumes the goal is hit every trading day, with no
            losing days, and leaves out spread, commission, swap and tax. A month is 52/12 weeks,
            so a year of five-day weeks is 260 trading days.
          </p>
        </>
      )}
    </div>
  );
}
