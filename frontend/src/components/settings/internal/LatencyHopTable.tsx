import { cn } from "@/lib/cn";
import { fmtMs, type Row } from "./latency_rows";
import { STATE_BG, STATE_TEXT, TICKS, logPct } from "./latency_viz";
import { LatencyProbeTile } from "./LatencyProbeTile";

/**
 * Every hop, probe and broker figure, in the order a signal travels.
 *
 * A single reading taken now (a probe) is a tile with a gauge against its
 * limit. A spread over many signals is a bar on the tab's shared log axis:
 * solid to the typical time, lighter to the p90, a tick at the worst. The
 * explanation of each row is its hover text, so the screen reads as numbers
 * and shapes first.
 */
export function LatencyHopTable({ rows, testId, stripPrefix = "" }: {
  rows: Row[];
  testId?: string;
  /** Dropped from displayed labels, for a block already titled with it. */
  stripPrefix?: string;
}) {
  if (rows.length === 0) return null;
  const tiles = rows.filter((r) => r.kind === "probe");
  const bars = rows.filter((r) => r.kind === "stats");
  const shown = (label: string) =>
    stripPrefix && label.startsWith(stripPrefix) ? label.slice(stripPrefix.length) : label;

  return (
    <div data-testid={testId} className="space-y-2.5">
      {tiles.length > 0 && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
          {tiles.map((r) => <LatencyProbeTile key={r.key} row={r} label={shown(r.label)} />)}
        </div>
      )}
      {bars.length > 0 && (
        <div className="overflow-x-auto">
          <div className="min-w-[36rem]">
            <ScaleHeader />
            {bars.map((r) => <BarRow key={r.key} row={r} label={shown(r.label)} />)}
          </div>
        </div>
      )}
    </div>
  );
}

const COLS = "grid grid-cols-[minmax(9rem,15rem)_minmax(8rem,1fr)_15rem] items-center gap-3";
const NUMS = "num grid grid-cols-[4rem_4rem_4rem_3rem] text-right";

function ScaleHeader() {
  return (
    <div className={cn(COLS, "pb-1 text-[9.5px] uppercase tracking-wider text-ink-3")}>
      <span>Hop</span>
      <div className="relative h-3">
        {TICKS.map(([ms, label]) => (
          <span key={ms} className="absolute -translate-x-1/2 normal-case tracking-normal"
            style={{ left: `${logPct(ms)}%` }}>{label}</span>
        ))}
      </div>
      <div className={NUMS}>
        <span>typical</span><span>p90</span><span>worst</span><span>n</span>
      </div>
    </div>
  );
}

function BarRow({ row: r, label }: { row: Row; label: string }) {
  const p50 = logPct(r.p50);
  const p90 = Math.max(p50, logPct(r.p90));
  return (
    <div data-testid={`hop-${r.key}`} data-state={r.state}
      className={cn(COLS, "border-t border-line/60 py-1.5 text-[11px]")}>
      <div className="min-w-0" title={[r.detail, r.state === "none" ? r.note : ""].filter(Boolean).join("\n") || undefined}>
        <div className="flex items-center gap-1.5">
          <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", STATE_BG[r.state])} />
          <span className={cn("truncate", r.state === "none" ? "text-ink-3" : "text-ink-1")}>{label}</span>
        </div>
        {/* An unmeasured row says so with its grey dot and dashes; its note
            is the hover text rather than the same sentence on every line. */}
        {r.note && r.state !== "none" && (
          <div className={cn("truncate pl-3 text-[10px]",
            r.state === "fail" ? "text-loss" : r.state === "slow" ? "text-warning" : "text-ink-3")}
            title={r.note}>
            {r.note}
          </div>
        )}
      </div>

      <div className="relative h-3.5 rounded-sm bg-surface-3/60">
        {TICKS.map(([ms]) => (
          <span key={ms} aria-hidden className="absolute inset-y-0 w-px bg-line"
            style={{ left: `${logPct(ms)}%` }} />
        ))}
        {r.p50 != null && (
          <>
            <span aria-hidden className={cn("absolute inset-y-0.5 left-0 rounded-sm opacity-90", STATE_BG[r.state])}
              style={{ width: `${p50}%` }} />
            <span aria-hidden className={cn("absolute inset-y-0.5 rounded-r-sm opacity-30", STATE_BG[r.state])}
              style={{ left: `${p50}%`, width: `${p90 - p50}%` }} />
          </>
        )}
        {r.max != null && (
          <span aria-hidden className="absolute inset-y-0 w-0.5 rounded bg-ink-2"
            style={{ left: `${logPct(r.max)}%` }} title={`worst ${fmtMs(r.max)}`} />
        )}
        {r.amber != null && r.state !== "none" && (
          <span aria-hidden className="absolute -inset-y-0.5 border-l border-dashed border-warning"
            style={{ left: `${logPct(r.amber)}%` }} title={`slow above ${fmtMs(r.amber)}`} />
        )}
      </div>

      <div className={NUMS}>
        <span className={cn("font-semibold", STATE_TEXT[r.state])}>{fmtMs(r.p50)}</span>
        <span className="text-ink-2">{fmtMs(r.p90)}</span>
        <span className="text-ink-2">{fmtMs(r.max)}</span>
        <span className="text-ink-3">{r.n ?? ""}</span>
      </div>
    </div>
  );
}
