import { EmptyState } from "@/components/shared/EmptyState";
import { LatencyCheckerSection } from "../internal/LatencyCheckerSection";
import { LatencyOverview } from "../internal/LatencyOverview";
import { useLatencyController } from "../hooks/useLatencyController";

/**
 * Where the time goes between a signal and an MT5 fill (docs/todo/006).
 *
 * The overview answers first; then two checkers, as asked for: a Telegram
 * signal through parsing and the EA/bridge to MT5, and the signal generator
 * (the engines) to MT5. Each draws its route, then its hops as measured on
 * real signals since the app started, then the live checks along its path,
 * then -- when paired -- the VPS.
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
    <div className="space-y-4">
      <LatencyOverview pipelines={c.pipelines} result={c.result} paired={c.paired}
        running={c.running} error={c.error} onRun={() => void c.runCheck()} />

      <LatencyCheckerSection
        id="telegram"
        index={1}
        title="Telegram signal → parsing → EA/bridge → MT5"
        path={["Telegram", "Parsing", "EA / bridge", "MT5"]}
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
        index={2}
        title="Signal generator → EA/bridge → MT5"
        path={["Signal generator", "EA / bridge", "MT5"]}
        intro="Breakout and Reversal Engine signals, from the engine's decision to the confirmed order."
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
