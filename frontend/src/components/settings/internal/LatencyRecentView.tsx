import { formatClock } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { fmtMs, type PipelineView } from "./latency_rows";

const SHOWN = 10;

/** The newest signals hop by hop: where one particular delay happened. */
export function LatencyRecentView({ view, testId }: { view: PipelineView | undefined; testId: string }) {
  const hops = view?.hops ?? [];
  const recent = (view?.recent ?? []).slice(0, SHOWN);
  if (recent.length === 0) return null;
  return (
    <details data-testid={testId} className="text-[11px]">
      <summary className="cursor-pointer text-ink-2">Recent signals ({recent.length})</summary>
      <div className="overflow-x-auto">
        <table className="num mt-1 w-full">
          <thead>
            <tr className="text-left text-ink-3">
              <th className="pr-2 font-normal">at</th>
              <th className="pr-2 font-normal">signal</th>
              {hops.map((h) => (
                <th key={h.id} className="pr-2 text-right font-normal" title={h.label}>{h.id}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {recent.map((r) => (
              <tr key={r.key} className="border-t border-line">
                <td className="pr-2 text-ink-3">{formatClock(r.at)}</td>
                <td className="pr-2 text-ink-2">{r.label || r.key}</td>
                {hops.map((h) => {
                  const v = r.hops[h.id];
                  return (
                    <td key={h.id} className={cn("whitespace-nowrap pr-2 text-right",
                      v != null && v > h.amber_ms ? "text-warning" : "text-ink-1")}>
                      {fmtMs(v)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
