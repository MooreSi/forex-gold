import { cn } from "@/lib/cn";
import { fmtMs, type PipelineView } from "./latency_rows";
import { STATE_BG, STATE_TEXT, hopState, legs, logPct } from "./latency_viz";

/** A leg's name up to its colon: "Order: gates, sizing, ..." reads "Order". */
function short(label: string): string {
  return label.split(":")[0];
}

const SHARE = ["bg-series-3", "bg-series-2", "bg-series-5", "bg-series-4", "bg-series-7", "bg-series-6"];

/**
 * The route as a picture: one segment per leg, thicker the longer it takes,
 * coloured by state, dashed where no signal has crossed it yet. Under it, the
 * share of the typical time each measured leg accounts for, which is the
 * question this tab is usually opened to answer.
 */
export function LatencyPipeline({ view }: { view: PipelineView | undefined }) {
  const hops = legs(view);
  if (hops.length === 0) return null;
  const timed = hops.filter((h) => h.stats?.n != null && (h.stats.p50 ?? 0) > 0);
  const sum = timed.reduce((a, h) => a + (h.stats.p50 ?? 0), 0);
  const shares = timed
    .map((h, i) => ({ h, pct: sum ? ((h.stats.p50 ?? 0) / sum) * 100 : 0, colour: SHARE[i % SHARE.length] }));

  return (
    <div className="space-y-3">
      <ol className="flex overflow-x-auto pb-1" aria-label="Route">
        {hops.map((h, i) => {
          const s = hopState(h);
          const thick = s === "none" ? 0 : 2 + (logPct(h.stats.p50) / 100) * 6;
          const last = i === hops.length - 1;
          return (
            <li key={h.id} className="flex min-w-[6.5rem] flex-1 flex-col items-center"
              title={`${h.label}${h.detail ? `\n${h.detail}` : ""}`}>
              <span className={cn("num text-xs font-semibold", STATE_TEXT[s])}>
                {fmtMs(h.stats?.p50)}
              </span>
              <div className="my-1.5 flex h-3 w-full items-center">
                <Node />
                {s === "none"
                  ? <span className="mx-0.5 flex-1 border-t-2 border-dashed border-ink-3/40" />
                  : <span className={cn("mx-0.5 flex-1 rounded-full", STATE_BG[s])}
                      style={{ height: `${thick}px` }} />}
                {last && <Node end />}
              </div>
              <span className="line-clamp-2 px-1 text-center text-[10px] leading-tight text-ink-2">
                {short(h.label)}
              </span>
            </li>
          );
        })}
      </ol>

      <div>
        <div className="mb-1 flex items-baseline justify-between text-[10px] uppercase tracking-wider text-ink-3">
          <span>Where the time goes</span>
          {sum > 0 && <span className="num normal-case tracking-normal">{fmtMs(sum)} across {timed.length} timed legs</span>}
        </div>
        {sum > 0 ? (
          <>
            <div className="flex h-2.5 overflow-hidden rounded-full bg-surface-3">
              {shares.map(({ h, pct, colour }) => (
                <span key={h.id} className={cn("h-full", colour)} style={{ width: `${pct}%` }}
                  title={`${h.label}: ${fmtMs(h.stats.p50)} (${pct.toFixed(1)}%)`} />
              ))}
            </div>
            <ul className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[10.5px]">
              {shares.filter((x) => x.pct >= 1).map(({ h, pct, colour }) => (
                <li key={h.id} className="flex items-center gap-1.5">
                  <span aria-hidden className={cn("size-2 rounded-sm", colour)} />
                  <span className="text-ink-2">{short(h.label)}</span>
                  <span className="num font-semibold text-ink-1">{pct.toFixed(pct >= 99.95 ? 0 : 1)}%</span>
                </li>
              ))}
              {shares.some((x) => x.pct < 1) && (
                <li className="text-ink-3">others under 1%</li>
              )}
            </ul>
          </>
        ) : (
          <div className="rounded-md border border-dashed border-line px-3 py-2 text-[11px] text-ink-3">
            No signal has been timed along this route since the app started.
          </div>
        )}
      </div>
    </div>
  );
}

function Node({ end = false }: { end?: boolean }) {
  return (
    <span aria-hidden className={cn("size-2.5 shrink-0 rounded-full border-2 border-ink-3 bg-surface-1",
      end && "border-accent bg-accent")} />
  );
}
