import { useState } from "react";
import { Pause, Play } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";

interface PauseControlProps {
  paused: boolean;
  onChanged: () => void;
}

/**
 * Stop new orders by hand, and start them again.
 *
 * Restored 2026-09-18. The NiceGUI header had this behind a pause icon; the
 * React port dropped it, so between then and now there was **no way to halt
 * trading from the dashboard** — an operator who wanted to stop had to disable
 * things one at a time or edit the database.
 *
 * Pausing is the safe direction and takes one press plus a confirm. Resuming
 * is the direction that lets money move again, and it does more than clear a
 * flag: the backend re-arms the post-close guards, because otherwise a resume
 * after a give-back halt lasts exactly until the next trade closes.
 *
 * The wording is the original's: what a pause does NOT stop is the part people
 * get wrong. Generators keep running and open trades keep being managed.
 */
export function PauseControl({ paused, onChanged }: PauseControlProps) {
  const [open, setOpen] = useState(false);
  const [hours, setHours] = useState("4");
  const [until, setUntil] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(path: string, body: unknown) {
    setBusy(true);
    setError(null);
    try {
      await api.post(path, body);
      setOpen(false);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function pause() {
    // A typed moment wins over the hours field, and an empty hours box must
    // never become 0 — that is a pause already in the past, which the backend
    // refuses but which would read here as "nothing happened".
    if (until.trim()) {
      const ts = Date.parse(until.trim().replace(" ", "T"));
      if (Number.isNaN(ts)) {
        setError("That is not a date and time. Use YYYY-MM-DD HH:MM.");
        return;
      }
      void send("/api/trading/pause", { until: ts / 1000 });
      return;
    }
    void send("/api/trading/pause", { hours: Number(hours) || 4 });
  }

  return (
    <>
      <Button
        variant="ghost"
        onClick={() => { setError(null); setOpen(true); }}
        title={paused ? "Trading is paused. Resume it." : "Stop sending new orders."}
      >
        {paused
          ? <span className="flex items-center gap-1 text-warning"><Play size={13} /> Paused</span>
          : <span className="flex items-center gap-1"><Pause size={13} /> Pause</span>}
      </Button>

      <DialogShell
        open={open}
        onOpenChange={setOpen}
        title={paused ? "Resume trading?" : "Pause trading"}
      >
        <div className="space-y-3 text-xs text-ink-2">
          <p>
            While paused, all signal generators and Telegram signals continue to
            run normally but no orders will be sent to MT5. Active trade
            management (SL/TP monitoring) continues as normal.
          </p>

          {paused ? (
            <p className="text-ink-3">
              Resuming also restarts the post-close guards' windows from now.
              Without that, resuming after a give-back halt would be undone by
              the next trade that closes.
            </p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                Pause for (hours)
                <input
                  aria-label="Pause for (hours)"
                  className="num mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
                  value={hours}
                  onChange={(e) => setHours(e.target.value)}
                />
                <span className="mt-0.5 block text-[10px] text-ink-3">
                  0.25 is fifteen minutes.
                </span>
              </label>
              <label className="block">
                Or until (YYYY-MM-DD HH:MM)
                <input
                  aria-label="Or until (YYYY-MM-DD HH:MM)"
                  className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
                  value={until}
                  onChange={(e) => setUntil(e.target.value)}
                  placeholder="leave blank to use hours"
                />
              </label>
            </div>
          )}

          {error && <p role="alert" className="text-xs text-loss">{error}</p>}

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            {paused ? (
              <Button disabled={busy}
                onClick={() => void send("/api/trading/resume", {})}>
                {busy ? "Resuming…" : "Resume trading"}
              </Button>
            ) : (
              <Button disabled={busy} onClick={pause}>
                {busy ? "Pausing…" : "Pause now"}
              </Button>
            )}
          </div>
        </div>
      </DialogShell>
    </>
  );
}
