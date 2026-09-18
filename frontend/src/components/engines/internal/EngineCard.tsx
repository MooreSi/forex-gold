import { Play, Square } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { cn } from "@/lib/cn";
import type { EngineRow } from "../hooks/useEnginesController";

interface EngineCardProps {
  engine: EngineRow;
  busy: boolean;
  onSetRunning: (engine: string, running: boolean) => void;
}

/**
 * One engine, its state, and the switch.
 *
 * "Not built" and "built but stopped" are different states with different
 * answers — the first cannot be started at all — so they render differently
 * rather than both greying out the button.
 */
export function EngineCard({ engine, busy, onSetRunning }: EngineCardProps) {
  const notBuilt = !engine.built;
  return (
    <div
      data-testid={`engine-${engine.id}`}
      data-running={engine.running}
      data-built={engine.built}
      className="rounded border border-line bg-surface-2 p-3"
    >
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "h-2 w-2 rounded-full",
            engine.running ? "bg-profit" : notBuilt ? "bg-ink-3" : "bg-loss",
          )}
        />
        <h3 className="text-sm font-semibold text-ink-1">{engine.label}</h3>
      </div>
      <p className="mt-0.5 text-[11px] text-ink-3">
        {notBuilt
          ? "Not built on this install"
          : engine.running
            ? "Running — producing signals"
            : "Stopped"}
      </p>
      <Button
        className="mt-2"
        variant={engine.running ? "danger" : "success"}
        disabled={busy}
        disabledReason={
          notBuilt
            ? `The ${engine.label} engine is not built on this install, so there is nothing to start.`
            : null
        }
        onClick={() => onSetRunning(engine.id, !engine.running)}
      >
        {engine.running ? <Square size={12} /> : <Play size={12} />}
        {engine.running ? "Stop" : "Start"}
      </Button>
    </div>
  );
}
