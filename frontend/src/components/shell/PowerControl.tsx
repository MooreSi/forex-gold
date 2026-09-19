import { useState } from "react";
import { Power, PowerOff, RotateCw } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";

/**
 * Restart and Stop, restored from the NiceGUI header's power button.
 *
 * Neither closes a position. Open trades keep running to their own SL/TP on
 * the broker's side — exactly what happens when the machine is turned off —
 * but both end the process that is managing them, so the dialog says that
 * plainly and neither happens on one press.
 *
 * The two are one press apart in the same dialog, which is why the tests pin
 * that they hit different endpoints: sending a restart for a stop leaves the
 * app running while the operator believes it is off.
 */
export function PowerControl() {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function send(path: string, label: string) {
    setBusy(label);
    setError(null);
    try {
      const res = await api.post<{ result?: string }>(path, {});
      setNote(res.result ?? `${label} requested.`);
      setOpen(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <button
        type="button"
        data-testid="power-control"
        aria-label="Power options"
        onClick={() => { setError(null); setNote(null); setOpen(true); }}
        title="Power options — restart or stop the app"
        className="rounded p-1.5 text-profit transition-colors hover:bg-surface-2"
      >
        <Power size={15} />
      </button>

      <DialogShell open={open} onOpenChange={setOpen} title="Power options">
        <div className="space-y-3 text-xs text-ink-2">
          <p>
            Restart relaunches the app and your browser reconnects in a few
            seconds. Stop shuts it down — it <strong>does not start itself
            again</strong>, so you would launch it from the desktop shortcut.
          </p>
          <p className="text-ink-3">
            Neither closes anything. Positions that are already open keep
            running to their own SL/TP at the broker, the same as if the
            machine were switched off — but nothing on this side is managing
            them while the app is down.
          </p>

          {error && <p role="alert" className="text-xs text-loss">{error}</p>}

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button
              disabled={busy !== null}
              onClick={() => void send("/api/node/restart", "Restart")}
            >
              <RotateCw size={13} /> {busy === "Restart" ? "Restarting…" : "Restart"}
            </Button>
            <Button
              variant="danger"
              disabled={busy !== null}
              onClick={() => void send("/api/node/stop", "Stop")}
            >
              <PowerOff size={13} /> {busy === "Stop" ? "Stopping…" : "Stop"}
            </Button>
          </div>
        </div>
      </DialogShell>

      {note && (
        <span role="status" className="max-w-xs text-[11px] text-ink-3">{note}</span>
      )}
    </>
  );
}
