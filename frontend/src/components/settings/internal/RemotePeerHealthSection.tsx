import { asObject } from "@/lib/asArray";

/** Green, amber or red, at the thresholds the NiceGUI page used. */
function loadTone(percent: number): string {
  if (percent >= 85) return "text-loss";
  if (percent >= 60) return "text-warning";
  return "text-profit";
}

function engineLabel(name: string): string {
  return name.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/**
 * The remote node's own health, from its 3 s heartbeat.
 *
 * On a headless VPS there is no other screen that says any of this. The
 * NiceGUI page showed it; the port kept the balance and dropped the rest.
 */
export function RemotePeerHealthSection({ peer }: { peer: Record<string, unknown> }) {
  const engines = asObject<Record<string, unknown>>(peer.engines);
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
      {Object.keys(engines).length > 0 && (
        <p data-testid="remote-engines" className="text-ink-3">
          Engines:{" "}
          {Object.entries(engines)
            .map(([name, on]) => `${engineLabel(name)} ${on ? "on" : "off"}`)
            .join(" · ")}
        </p>
      )}
    </div>
  );
}
