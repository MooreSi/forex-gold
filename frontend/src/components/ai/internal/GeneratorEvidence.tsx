import { ArrowRight } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatSignedMoney, pnlColour } from "@/components/shared/format";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

/**
 * Each engine's early half against its late half.
 *
 * The question the whole subject exists for is "is it getting better", and a
 * single-period total cannot answer it. The gatherer splits the window in two
 * for exactly that reason, so the table shows both halves side by side with
 * the direction of travel between them.
 *
 * A half with no trades shows "—", not 0% — an engine that did not trade in
 * the first half has not declined, and an arrow drawn from a zero says it did.
 */
function n(value: unknown): number | null {
  if (value == null || value === "") return null;
  const v = typeof value === "number" ? value : Number(value);
  return Number.isFinite(v) ? v : null;
}

function Half({ stats, testId }: { stats: Record<string, unknown>; testId: string }) {
  const count = n(stats["count"]) ?? 0;
  if (count === 0) {
    return <span data-testid={testId} className="text-ink-3">—</span>;
  }
  const pnl = n(stats["total_pnl"]);
  return (
    <span data-testid={testId} className="num whitespace-nowrap">
      <span className="text-ink-2">{(n(stats["win_rate"]) ?? 0).toFixed(0)}%</span>
      <span className="text-ink-3"> of {count} · </span>
      <span className={pnlColour(pnl ?? 0)}>{formatSignedMoney(pnl)}</span>
    </span>
  );
}

export function GeneratorEvidence({ evidence }: { evidence: unknown }) {
  const data = asObject(evidence);
  const engines = asArray<Record<string, unknown>>(data["engines"]);

  if (engines.length === 0) {
    return <EmptyState title="No engine has closed a trade in this window" />;
  }

  return (
    <div className="space-y-2">
      <p className="text-[11px] text-ink-3">
        Each engine&apos;s first half of the window against its second — the only
        shape that can answer &ldquo;is it improving&rdquo;.
      </p>
      <div className="overflow-auto">
        <table data-testid="generator-engines" className="w-full text-left text-[11px]">
          <thead className="text-ink-3">
            <tr>
              {["Engine", "Early half", "", "Late half", "Whole window"]
                .map((h, i) => <th key={i} className="px-2 py-1 font-normal">{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {engines.map((e, i) => {
              const key = String(e["strategy"] ?? i);
              const early = asObject(e["early_half"]);
              const late = asObject(e["late_half"]);
              const all = asObject(e["all"]);
              const bothHalves = (n(early["count"]) ?? 0) > 0 && (n(late["count"]) ?? 0) > 0;
              const improving = bothHalves
                && (n(late["total_pnl"]) ?? 0) > (n(early["total_pnl"]) ?? 0);
              return (
                <tr key={key} data-testid={`engine-${key}`} className="border-t border-line">
                  <td className="px-2 py-1 text-ink-1">{String(e["label"] ?? key)}</td>
                  <td className="px-2 py-1"><Half stats={early} testId={`early-${key}`} /></td>
                  <td className="px-2 py-1">
                    {/* Only drawn when both halves have trades. An arrow from
                        a half that never traded is a trend that never was. */}
                    {bothHalves && (
                      <ArrowRight size={11} aria-hidden
                        className={cn(improving ? "text-profit" : "text-loss")} />
                    )}
                  </td>
                  <td className="px-2 py-1"><Half stats={late} testId={`late-${key}`} /></td>
                  <td className="px-2 py-1"><Half stats={all} testId={`all-${key}`} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
