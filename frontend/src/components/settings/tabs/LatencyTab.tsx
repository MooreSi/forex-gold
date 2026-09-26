import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { LatencyCheckerSection } from "../internal/LatencyCheckerSection";
import { useLatencyController } from "../hooks/useLatencyController";

/**
 * Where the time goes between a signal and an MT5 fill (docs/todo/006).
 *
 * Two checkers, as asked for: a Telegram signal through parsing and the
 * EA/bridge to MT5, and the signal generator (the engines) to MT5. Each lists
 * its hops as measured on real signals since the app started, then the live
 * checks along its path, then -- when paired -- the VPS.
 */
export function LatencyTab() {
  const c = useLatencyController();

  if (c.loading) return <EmptyState title="Loading" />;
  if (c.loadError && !c.pipelines) {
    return <EmptyState title="Could not load latency" hint={c.loadError} />;
  }

  const probes = c.result?.local.probes;
  const broker = c.result?.local.broker;
  const vps = c.result?.vps;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Button disabledReason={c.running ? "A check is already running." : null}
          onClick={() => void c.runCheck()}>
          {c.running ? "Checking…" : "Run check"}
        </Button>
        <span className="text-[11px] text-ink-3">
          Times each connection now. Read-only: it places, changes and closes nothing.
          Amber is slower than expected; red did not answer.
        </span>
        {c.error && <span className="text-[11px] text-loss">{c.error}</span>}
      </div>

      <LatencyCheckerSection
        id="telegram"
        title="1. Telegram signal → parsing → EA/bridge → MT5"
        intro="From the channel's post to the order being confirmed."
        view={c.pipelines?.telegram}
        probes={probes}
        probeKeys={["telegram", "loop", "db", "ea", "bridge", "tick"]}
        broker={broker}
        paired={c.paired}
        vps={vps}
        vpsPipelines={["forwarded", "telegram"]}
      />

      <LatencyCheckerSection
        id="engine"
        title="2. Signal generator → EA/bridge → MT5"
        intro="Breakout and Reversal Engine signals, from the engine's decision to the order being confirmed."
        view={c.pipelines?.engine}
        waits={c.structural}
        probes={probes}
        probeKeys={["loop", "db", "tick", "ea", "bridge"]}
        broker={broker}
        paired={c.paired}
        vps={vps}
        vpsPipelines={["forwarded", "engine"]}
      />
    </div>
  );
}
