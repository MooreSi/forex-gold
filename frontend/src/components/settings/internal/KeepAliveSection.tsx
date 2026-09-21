import { useState } from "react";
import { HeartPulse } from "lucide-react";
import { Tooltip } from "@/components/shared/Tooltip";

interface KeepAliveSectionProps {
  /** `/api/node/state.autostart`. */
  autostart: Record<string, unknown>;
  /** Writes the toggle. Rejects if the OS refuses; the reason is shown. */
  onChange: (enabled: boolean) => Promise<void>;
}

/**
 * Keep the app — and with it the bridge and MT5 — running.
 *
 * Owner, 2026-09-21: "missing the toggle to ensure the app, bridge and mt5 is
 * kept alive". It was not missing. It was one checkbox called "Start the app
 * when this machine boots", bound to `installed` — whether the OS scheduler
 * entry exists — which is not the same question as whether the operator
 * turned the feature on.
 *
 * **The switch follows the stored setting; the line under it reports the OS.**
 * Those disagree in exactly one case and it is the case worth showing: the
 * setting is on and the scheduler entry has been lost to an OS upgrade or a
 * machine migration. Bound to `installed`, that rendered as an unticked box —
 * indistinguishable from never having been switched on. This is the state the
 * NiceGUI page reported as "On, but the scheduler entry is missing — toggle
 * off and on to repair", and it is why this section exists.
 *
 * A *disarmed* watchdog is not a fault: `FOREX Stop.command` / `Stop
 * FOREX.bat` disarm it so that Stop genuinely stops, and starting the app
 * re-arms it. Reporting that as broken would teach the operator to ignore the
 * line that reports the real one.
 */
export function KeepAliveSection({ autostart, onChange }: KeepAliveSectionProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const supported = autostart["supported"] === true;
  const enabled = autostart["enabled"] === true;
  const installed = autostart["installed"] === true;
  const armed = autostart["armed"] === true;
  const everyMin = Math.max(
    1, Math.round(Number(autostart["check_interval_secs"] ?? 120) / 60));

  const status = !supported
    ? { text: "Not supported on this platform — nothing here can restart the app.",
        tone: "text-ink-3" }
    : !enabled
      ? { text: "Off — nothing will restart the app if it stops.", tone: "text-ink-3" }
      : !installed
        ? { text: "On, but the scheduler entry is missing — turn it off and on "
                + "again to repair it. Nothing is watching the app right now.",
            tone: "text-warning" }
        : !armed
          ? { text: `Paused — the app was stopped deliberately. Starting it again `
                  + `re-arms the watchdog, which then checks every ${everyMin} min.`,
              tone: "text-ink-2" }
          : { text: `Active — checking every ${everyMin} min.`, tone: "text-profit" };

  async function toggle(want: boolean) {
    setBusy(true); setError(null);
    try {
      await onChange(want);
    } catch (e) {
      // Deliberately does not move the switch. The stored setting is only
      // written once the OS accepted, so re-reading state shows the truth --
      // and a switch showing ON with no entry behind it is the false sense of
      // safety this feature exists to remove.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h3 className="flex items-center gap-1.5 text-xs font-semibold text-ink-1">
        <HeartPulse size={13} aria-hidden />
        Keep it running
      </h3>

      {supported && (
        <label className="mt-1 flex items-center gap-2 text-xs text-ink-2">
          <Tooltip label="An OS-level watchdog that relaunches the app, the bridge and MT5 if they stop. It is what keeps an unattended machine trading. A watchdog the Stop scripts have DISARMED is not a fault — that is how Stop genuinely stops.">
            <input
              type="checkbox"
              aria-label="Keep the app, the bridge and MT5 running"
              checked={enabled}
              disabled={busy}
              onChange={(e) => void toggle(e.target.checked)}
              className="accent-accent"
            />
          </Tooltip>
          Restart the app automatically if it stops
        </label>
      )}

      <p role="status" className={`mt-1 text-[11px] ${status.tone}`}>{status.text}</p>

      {error && (
        <p role="alert" className="mt-1 rounded border border-warning/40 bg-warning/10
                                   px-3 py-2 text-[11px] text-warning">
          Could not change it: {error}
        </p>
      )}

      <p data-testid="keep-alive-explains" className="mt-1 text-[11px] leading-snug text-ink-3">
        The operating system’s own scheduler — launchd on macOS, Task Scheduler
        on Windows — checks every {everyMin} min that the app is still serving
        on its port, and starts it again if it is not. It runs that check at
        login and after a reboot too, so the machine comes back by itself.
        Starting the app is what brings the MT5 bridge and MT5 back with it,
        and the app’s own bridge watchdog reconnects the bridge while it is
        running. Stopping the app on purpose still stops it.
      </p>
    </section>
  );
}
