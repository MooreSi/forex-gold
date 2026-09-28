import { formatClock } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { fmtMs, type PipelineView } from "./latency_rows";
import { logPct } from "./latency_viz";

const SHOWN = 10;

/**
 * The newest signals hop by hop, as a heat map: where one particular delay
 * happened. The deeper the green the longer the hop; amber is over its limit.
 * A message that no hop timed (a reply, an edit) is left out and counted.
 */
export function LatencyRecentView({ view, testId }: { view: PipelineView | undefined; testId: string }) {
  const hops = view?.hops ?? [];
  const all = view?.recent ?? [];
  const timed = all.filter((r) => Object.values(r.hops).some((v) => v != null));
  const recent = timed.slice(0, SHOWN);
  if (recent.length === 0) return null;
  const skipped = all.length - timed.length;
  return (
    <details data-testid={testId} className="group rounded-lg border border-line bg-surface-2/50 text-[11px]">
      <summary className="cursor-pointer select-none px-3 py-2 font-medium text-ink-2">
        Recent signals ({recent.length})
        {skipped > 0 && <span className="font-normal text-ink-3"> · {skipped} untimed messages hidden</span>}
      </summary>
      <div className="overflow-x-auto px-3 pb-3">
        <table className="num w-full border-separate border-spacing-0.5">
          <thead>
            <tr className="text-left text-[10px] text-ink-3">
              <th className="pr-2 font-normal">at</th>
              <th className="pr-2 font-normal">signal</th>
              {hops.map((h) => (
                <th key={h.id} className="px-1 text-center font-normal" title={h.label}>{h.id}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {recent.map((r) => (
              <tr key={r.key}>
                <td className="pr-2 text-ink-3">{formatClock(r.at)}</td>
                <td className="max-w-[12rem] truncate pr-2 text-ink-2" title={r.label || r.key}>{r.label || r.key}</td>
                {hops.map((h) => {
                  const v = r.hops[h.id];
                  const over = v != null && v > h.amber_ms;
                  return (
                    <td key={h.id}
                      className={cn("whitespace-nowrap rounded px-1.5 py-0.5 text-right",
                        v == null ? "text-ink-3" : over ? "bg-warning/20 text-warning" : "text-ink-1")}
                      style={v != null && !over
                        ? { background: `color-mix(in srgb, var(--color-profit) ${6 + logPct(v) * 0.3}%, transparent)` }
                        : undefined}>
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
