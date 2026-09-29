import { api } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { asObject } from "@/lib/asArray";

/** Green, amber or red, at the thresholds the NiceGUI page used. */
function loadTone(percent: number): string {
  if (percent >= 85) return "text-loss";
  if (percent >= 60) return "text-warning";
  return "text-profit";
}

/**
 * The engines this build has, in the order they are shown.
 *
 * A fixed list rather than whatever the heartbeat carries: a VPS on an older
 * build still reports `bounce`, whose code was deleted on 2026-09-14, and the
 * owner asked for it to be gone from this page (2026-09-28).
 */
const ENGINES: { id: string; label: string }[] = [
  { id: "breakout", label: "Breakout" },
  { id: "reversal_engine", label: "Reversal engine" },
];

/**
 * The remote node's own health, from its 3 s heartbeat.
 *
 * On a headless VPS there is no other screen that says any of this. The
 * NiceGUI page showed it; the port kept the balance and dropped the rest.
 *
 * Start/Stop go through /api/engines/running, which routes to the node that
 * is actually trading. Offered only while that is the VPS: otherwise the
 * command would land on this machine while the label named the VPS's engine.
 */
export function RemotePeerHealthSection({
  peer, busy = false, act,
}: {
  peer: Record<string, unknown>;
  busy?: boolean;
  act?: (fn: () => Promise<{ note?: string }>) => Promise<void>;
}) {
  const engines = asObject<Record<string, unknown>>(peer.engines);
  const shown = ENGINES.filter((e) => e.id in engines);
  const canControl = act !== undefined && peer.active_trader === "remote_vps";
  const cpu = typeof peer.cpu_percent === "number" ? peer.cpu_percent : null;
  const memPct = Number(peer.mem_percent ?? 0);
  const memUsed = Number(peer.mem_used_mb ?? 0);
  const memTotal = Number(peer.mem_total_mb ?? 0);
  const whole = (n: number) => Math.round(n).toLocaleString("en-GB");

  return (
    <div data-testid="remote-health" className="space-y-0.5">
      <p className="flex flex-wrap gap-x-4">
        {cpu !== null && (
          <>
            <span data-testid="remote-cpu" className={`num font-semibold ${loadTone(cpu)}`}>
              CPU {Math.round(cpu)}%
            </span>
            <span className={`num font-semibold ${loadTone(memPct)}`}>
              Memory {whole(memUsed)} / {whole(memTotal)} MB ({Math.round(memPct)}%)
            </span>
          </>
        )}
        {"ea_connected" in peer && (
          <span className={peer.ea_connected ? "text-profit" : "text-ink-3"}>
            {peer.ea_connected ? "EA connected" : "EA not connected"}
          </span>
        )}
      </p>
      {shown.length > 0 && (
        <div data-testid="remote-engines" className="flex flex-wrap items-center gap-x-4 gap-y-1 text-ink-3">
          <span>Engines:</span>
          {shown.map(({ id, label }) => {
            const on = Boolean(engines[id]);
            return (
              <span key={id} className="flex items-center gap-1.5">
                <span className={on ? "text-profit" : "text-ink-3"}>
                  {label} {on ? "on" : "off"}
                </span>
                {canControl && (
                  <Button
                    variant="ghost"
                    disabled={busy}
                    aria-label={`${on ? "Stop" : "Start"} ${label}`}
                    onClick={() => void act!(() =>
                      api.post("/api/engines/running", { engine: id, running: !on }))}
                  >
                    {on ? "Stop" : "Start"}
                  </Button>
                )}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}
