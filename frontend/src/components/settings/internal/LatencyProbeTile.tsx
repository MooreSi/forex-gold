import { cn } from "@/lib/cn";
import { fmtMs, type Row } from "./latency_rows";
import { STATE_TEXT, STATE_WORD } from "./latency_viz";

const R = 15;
const C = 2 * Math.PI * R;

/**
 * One live reading: the time, and a ring showing how much of its limit it
 * used. A full red ring with no number is a check that did not answer; the
 * reason is printed under it, never only in a tooltip.
 */
export function LatencyProbeTile({ row: r, label }: { row: Row; label: string }) {
  const ratio = r.state === "fail" ? 1
    : r.p50 != null && r.amber ? Math.min(1, r.p50 / r.amber) : 0;
  return (
    <div data-testid={`hop-${r.key}`} data-state={r.state}
      title={[r.detail, r.amber != null ? `Slow above ${fmtMs(r.amber)}.` : ""].filter(Boolean).join(" ")}
      className={cn("flex min-w-0 flex-col gap-1 rounded-lg border bg-surface-1 p-2.5",
        r.state === "fail" ? "border-loss/50" : r.state === "slow" ? "border-warning/50" : "border-line")}>
      <div className="flex items-center gap-2.5">
        <svg viewBox="0 0 40 40" className={cn("size-9 shrink-0 -rotate-90", STATE_TEXT[r.state])} aria-hidden>
          <circle cx="20" cy="20" r={R} fill="none" stroke="currentColor" strokeOpacity="0.15" strokeWidth="4" />
          <circle cx="20" cy="20" r={R} fill="none" stroke="currentColor" strokeWidth="4"
            strokeLinecap="round" strokeDasharray={`${Math.max(ratio * C, 2)} ${C}`} />
        </svg>
        <div className="min-w-0">
          <div className={cn("num text-base font-semibold leading-tight", STATE_TEXT[r.state])}>
            {fmtMs(r.p50)}
          </div>
          <div className="text-[9.5px] uppercase tracking-wider text-ink-3">{STATE_WORD[r.state]}</div>
        </div>
      </div>
      <div className="line-clamp-2 text-[11px] leading-snug text-ink-2">{label}</div>
      {r.note && (
        <div className={cn("text-[10px] leading-snug", r.state === "fail" ? "text-loss" : "text-ink-3")}>
          {r.note}
        </div>
      )}
    </div>
  );
}
