import { useState } from "react";
import { RotateCw } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { Notice } from "@/components/shared/Notice";
import { useNoteUntilPeerReturns } from "../hooks/useNoteUntilPeerReturns";

// The fallback for a VPS that never drops the link; a restart takes about a
// minute, so this is well past it.
const NOTE_TTL_MS = 5 * 60_000;

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
  const { note, setNote, clear } = useNoteUntilPeerReturns(connected);

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
      {note && (
        <div className="mt-1"><Notice ttlMs={NOTE_TTL_MS} onDismiss={clear}>{note}</Notice></div>
      )}

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
