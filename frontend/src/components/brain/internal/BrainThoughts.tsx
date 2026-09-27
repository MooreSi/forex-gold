import { formatClock } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { sentence, type BrainEvent, type BrainGate } from "../content/brainLayout";

const DOT: Record<string, string> = {
  executed: "bg-profit", queued: "bg-warning", blocked: "bg-loss",
};

/** The brain's running commentary: one plain line per decision, newest first.
 *  Every word comes from what the app recorded; nothing is generated. */
export function BrainThoughts({ events, gates, limit = 14 }: {
  events: BrainEvent[]; gates: BrainGate[]; limit?: number;
}) {
  const rows = [...events].sort((a, b) => b.ts - a.ts).slice(0, limit);
  if (rows.length === 0) {
    return <p className="text-xs text-ink-3">No decisions recorded yet.</p>;
  }
  return (
    <ol className="space-y-1.5" aria-label="Recent decisions">
      {rows.map((e) => (
        <li key={e.key} data-testid={`brain-thought-${e.key}`}
          className="flex gap-2 text-[11.5px] leading-snug">
          <span className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", DOT[e.outcome])} />
          <span className="num shrink-0 text-ink-3">{formatClock(e.ts)}</span>
          <span className="min-w-0 break-words text-ink-2">{sentence(e, gates)}</span>
        </li>
      ))}
    </ol>
  );
}
