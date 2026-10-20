import { useState } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { StatCard } from "@/components/shared/StatCard";
import { formatMoney, formatPercent } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { useMonteCarlo, type MonteCarlo } from "../hooks/useMonteCarlo";

const HORIZONS: Array<number | null> = [null, 100, 250, 500];
const pct = (p: number | undefined) => formatPercent(p == null ? null : p * 100);

/**
 * How bad the next N trades could get, resampled from this window's own
 * closed trades (docs/todo/024). The answer pros size from: not the average
 * trade, but the odds of a drawdown deep enough to stop you trading.
 */
export function MonteCarloSection({ days }: { days: number }) {
  const [horizon, setHorizon] = useState<number | null>(null);
  const poll = useMonteCarlo(days, horizon);
  const mc: MonteCarlo | null = poll.data;

  if (!mc) {
    return <EmptyState title={poll.error ? "Could not run the simulation" : "Simulating"}
      hint={poll.error?.message} />;
  }

  return (
    <div className="space-y-3" data-testid="monte-carlo">
      <div className="flex flex-wrap items-center gap-1 text-[11px] text-ink-3">
        Next
        {HORIZONS.map((h) => (
          <button key={h ?? "window"} onClick={() => setHorizon(h)} aria-pressed={h === horizon}
            className={cn("num rounded px-2 py-1 transition-colors",
              h === horizon ? "bg-surface-3 text-ink-1" : "hover:bg-surface-2 hover:text-ink-2")}>
            {h == null ? "as many as this window" : `${h} trades`}
          </button>
        ))}
      </div>
      {mc.note ? (
        <EmptyState title="Not enough to simulate" hint={mc.note} />
      ) : (
        <>
          <div className="grid gap-2 sm:grid-cols-4">
            <StatCard label="Drawdown ≥ 10%" value={pct(mc.prob_dd?.["10"])} valueClassName="text-loss"
              hint="Share of simulated paths whose balance fell 10% or more from a peak at some point." />
            <StatCard label="Drawdown ≥ 20%" value={pct(mc.prob_dd?.["20"])} valueClassName="text-loss" />
            <StatCard label="Drawdown ≥ 30%" value={pct(mc.prob_dd?.["30"])} valueClassName="text-loss" />
            <StatCard label="Drawdown ≥ 50%" value={pct(mc.prob_dd?.["50"])} valueClassName="text-loss" />
            <StatCard label="Typical worst drawdown" value={formatPercent(mc.max_dd_pct?.p50)}
              hint="The median of each path's worst fall from a peak." />
            <StatCard label="Bad-case drawdown (95th)" value={formatPercent(mc.max_dd_pct?.p95)}
              hint="One path in twenty fell further than this." />
            <StatCard label="Ends below today" value={pct(mc.prob_loss)} />
            <StatCard label="Account lost" value={pct(mc.prob_ruin)} />
          </div>
          <table className="w-full max-w-xl text-xs">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
                <th className="py-1 font-medium">Balance after {mc.horizon} trades</th>
                <th className="py-1 font-medium">5th</th><th className="py-1 font-medium">25th</th>
                <th className="py-1 font-medium">Median</th><th className="py-1 font-medium">75th</th>
                <th className="py-1 font-medium">95th</th>
              </tr>
            </thead>
            <tbody className="num">
              <tr className="border-t border-line">
                <td className="py-1.5 text-ink-3">from {formatMoney(mc.balance)}</td>
                {(["p5", "p25", "p50", "p75", "p95"] as const).map((k) => (
                  <td key={k} className="py-1.5 text-ink-1">{formatMoney(mc.final?.[k])}</td>
                ))}
              </tr>
            </tbody>
          </table>
          <p className="text-[11px] leading-relaxed text-ink-3">
            {mc.paths?.toLocaleString("en-GB")} paths, each drawing {mc.horizon} trades at random
            from the {mc.trades} closed in the last {days} days (average {formatMoney(mc.mean_trade)} a
            trade, fees in). Two assumptions: each trade is drawn at the size it was taken, and
            trades are drawn independently, so losing streaks that cluster in one bad regime are
            understated. Read the drawdown odds as a floor, not a ceiling.
          </p>
        </>
      )}
    </div>
  );
}
