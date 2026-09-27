import { useState } from "react";
import { RotateCw } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";

/**
 * Restart the VPS from here, so nobody has to log in to it (owner,
 * 2026-09-26). It restarts the way /restartapp does.
 *
 * Like the header's own Restart it closes nothing: positions keep their SL/TP
 * at the broker, but the VPS manages none of them until it is back. So it
 * never happens on one press, and the confirmation counts what is open.
 */
export function RemoteRestartSection({
  connected, openPositions,
}: { connected: boolean; openPositions: number }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function restart() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<{ note?: string }>("/api/remote/restart-vps", {});
      setNote(res.note ?? "VPS restarting.");
      setOpen(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pt-1">
      <Button
        variant="ghost"
        disabled={busy}
        disabledReason={connected ? undefined : "Not connected to the VPS."}
        onClick={() => { setError(null); setNote(null); setOpen(true); }}
      >
        <RotateCw size={13} /> Restart VPS
      </Button>
      {note && <p role="status" className="mt-1 text-[11px] text-profit">{note}</p>}

      <DialogShell open={open} onOpenChange={setOpen} title="Restart the VPS">
        <div className="space-y-3 text-xs text-ink-2">
          <p>
            The VPS app restarts and reconnects to this machine in about a
            minute. Nothing is closed.
          </p>
          <p className="text-ink-3">
            {openPositions > 0
              ? `${openPositions} open position${openPositions === 1 ? "" : "s"} keep their SL/TP at the broker, but the VPS manages none of them until it is back.`
              : "The VPS has no open positions."}
          </p>

          {error && <p role="alert" className="text-xs text-loss">{error}</p>}

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button disabled={busy} onClick={() => void restart()}>
              <RotateCw size={13} /> {busy ? "Restarting the VPS…" : "Restart the VPS"}
            </Button>
          </div>
        </div>
      </DialogShell>
    </div>
  );
}
