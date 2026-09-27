import { useCallback } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";
import { DashCard } from "./DashCard";

export interface FillCostGroup {
  strategy?: string;
  n: number;
  median_cost_pts: number | null;
  p75_cost_pts: number | null;
  p90_cost_pts: number | null;
  median_slippage_pts: number | null;
  adverse_share: number | null;
  favourable_share: number | null;
  median_spread_pts: number | null;
  median_cost_r: number | null;
}

export interface FillCostReport extends FillCostGroup {
  days: number;
  unmeasured: number;
  by_strategy: FillCostGroup[];
}

function pts(v: number | null | undefined): string {
  return v == null ? "—" : `${v.toFixed(2)} pts`;
}

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${Math.round(v * 100)}%`;
}

function Figure({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10.5px] uppercase tracking-wide text-ink-3">{label}</div>
      <div className="num text-sm text-ink-1" title={hint}>{value}</div>
    </div>
  );
}

/**
 * What fills really cost, measured per closed trade (broker/tca.py): spread,
 * slippage against the price asked for, and the round trip in points and as a
 * share of the stop. An unmeasured fill is counted, never treated as free.
 */
export function FillCostView({ report }: { report: FillCostReport | null | undefined }) {
  if (!report) return <p className="text-xs text-ink-3">Loading fill costs…</p>;
  if (report.n === 0) {
    return <p className="text-xs text-ink-3">No measured fills in the last {report.days} days.</p>;
  }
  const groups = asArray<FillCostGroup>(report.by_strategy);
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Figure label="Round trip (median)" value={pts(report.median_cost_pts)}
          hint={`p75 ${pts(report.p75_cost_pts)}, p90 ${pts(report.p90_cost_pts)}`} />
        <Figure label="Slippage (median)" value={pts(report.median_slippage_pts)} />
        <Figure label="Slipped against us" value={pct(report.adverse_share)}
          hint={`In our favour: ${pct(report.favourable_share)}`} />
        <Figure label="Spread (median)" value={pts(report.median_spread_pts)} />
      </div>
      <p data-testid="fill-cost-summary" className="text-[11px] text-ink-3">
        p90 round trip {pts(report.p90_cost_pts)} · median {pct(report.median_cost_r)} of the stop ·{" "}
        {report.n} fills{report.unmeasured ? `, ${report.unmeasured} not measurable` : ""}
      </p>
      {groups.length > 0 && (
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-left text-ink-3">
              <th className="py-0.5 font-normal">Strategy</th>
              <th className="py-0.5 font-normal">Fills</th>
              <th className="py-0.5 font-normal">Round trip</th>
              <th className="py-0.5 font-normal">Against us</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.strategy} data-testid={`fill-cost-${g.strategy}`}>
                <td className="max-w-[12rem] truncate py-0.5 text-ink-2" title={g.strategy}>
                  {(g.strategy ?? "").replace(/^template:/, "")}
                </td>
                <td className="num py-0.5 text-ink-2">{g.n}</td>
                <td className="num py-0.5 text-ink-2">{pts(g.median_cost_pts)}</td>
                <td className="num py-0.5 text-ink-2">{pct(g.adverse_share)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function FillCostCard() {
  const poll = usePoll<FillCostReport>(
    "fills/cost",
    useCallback(() => api.get<FillCostReport>("/api/fills/cost?days=14"), []),
    60_000,
  );
  return (
    <DashCard title="Fill cost" icon="activity" badge="14 days"
      footnote="Measured per closed trade against the price asked for. Positive slippage is against us.">
      <div className="p-3">
        {poll.error && !poll.data
          ? <p className="text-xs text-loss">Could not read fill costs.</p>
          : <FillCostView report={poll.data} />}
      </div>
    </DashCard>
  );
}
