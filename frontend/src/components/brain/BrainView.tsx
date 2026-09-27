import { useMemo } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { pickSources, type BrainEvent, type BrainGate } from "./content/brainLayout";
import { BrainGraph } from "./internal/BrainGraph";
import { BrainThoughts } from "./internal/BrainThoughts";

export interface BrainViewProps {
  gates: BrainGate[] | undefined;
  events: BrainEvent[] | undefined;
  error?: string | null;
  large?: boolean;
}

/**
 * The brain: a live map of every entry decision the app records (docs/todo/008).
 *
 * Read-only. It shows what the order paths decided and which gates are
 * holding orders now; it has no control and changes nothing. What it draws
 * comes from the backend's classification of the recorded reason, so a pulse
 * stops where the app really stopped the signal.
 */
export function BrainView({ gates, events, error, large }: BrainViewProps) {
  const g = asArray<BrainGate>(gates);
  const ev = asArray<BrainEvent>(events);
  const sources = useMemo(() => pickSources(ev), [ev]);

  if (g.length === 0) {
    return <EmptyState title={error ? "Could not read the brain" : "Waking up"} hint={error ?? undefined} />;
  }

  const holding = g.filter((x) => x.blocking === true);
  const executed = ev.filter((e) => e.outcome === "executed").length;

  return (
    <div className={cn("grid gap-3", large ? "h-full lg:grid-cols-4" : "lg:grid-cols-3")}>
      <div className={cn("min-w-0", large ? "lg:col-span-3" : "lg:col-span-2")}>
        <div className="mb-1 flex flex-wrap items-center gap-3 text-[11px] text-ink-3">
          <span><span className="text-profit">●</span> sent to broker</span>
          <span><span className="text-warning">●</span> queued</span>
          <span><span className="text-loss">●</span> held</span>
          <span className="num">{executed} of {ev.length} recent decisions reached the broker</span>
        </div>
        <div className={large ? "h-[70vh]" : "h-72"}>
          <BrainGraph gates={g} events={ev} sources={sources} />
        </div>
        <p data-testid="brain-holding" className={cn("mt-1 text-[11.5px]",
          holding.length ? "text-loss" : "text-ink-3")}>
          {holding.length
            ? `Holding orders now: ${holding.map((x) => x.detail ? `${x.label} (${x.detail})` : x.label).join("; ")}`
            : "No gate is holding orders right now."}
        </p>
      </div>
      <div className={cn("min-w-0 overflow-auto", large ? "max-h-[78vh]" : "max-h-80")}>
        <BrainThoughts events={ev} gates={g} limit={large ? 30 : 14} />
      </div>
    </div>
  );
}
