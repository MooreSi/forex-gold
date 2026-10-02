import { useCallback } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";
import { DashCard } from "./DashCard";
import { nodeBadge, nodeReadError, type ReportNode } from "./nodeLabel";

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
  /** The node the figures came from: the one that trades. */
  node?: ReportNode;
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
    <div className="gap-6 space-y-2 lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)] lg:space-y-0">
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
      </div>
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
                <td className="py-0.5">
                  <div className="flex items-center gap-2">
                    <span className="num w-9 text-ink-2">{pct(g.adverse_share)}</span>
                    {g.adverse_share != null && (
                      <span aria-hidden className="h-1.5 w-20 overflow-hidden rounded-full bg-surface-3">
                        <span className={g.adverse_share > 0.5 ? "block h-full bg-warning" : "block h-full bg-profit"}
                          style={{ width: `${Math.round(g.adverse_share * 100)}%` }} />
                      </span>
                    )}
                  </div>
                </td>
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
    <DashCard title="Fill cost" icon="activity" badge={nodeBadge("14 days", poll.data?.node)}
      footnote="Measured per closed trade against the price asked for. Positive slippage is against us.">
      {poll.error && !poll.data
        ? <p className="text-xs text-loss">{nodeReadError("fill costs", poll.error)}</p>
        : <FillCostView report={poll.data} />}
    </DashCard>
  );
}
