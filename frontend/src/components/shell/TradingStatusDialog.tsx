import { useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { Tooltip } from "@/components/shared/Tooltip";

/** What `/api/trading/status-badge` answers. The backend decides all of it. */
export interface TradingStatus {
  state: string;
  label: string;
  detail: string;
  until: number | null;
  resume_ts: number | null;
  can_resume: boolean;
}

interface TradingStatusDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  status: TradingStatus;
  onChanged: () => void;
}

/**
 * Stop new orders by hand, and start them again.
 *
 * The header used to carry two controls for this: a Pause button with its own
 * dialog, and this badge with a resume-only popover. Two controls for one
 * fact, on a bar that already runs out of room at 1024px — and the Pause
 * button knew only about the governor's manual pause, so with a profit target
 * reached it offered "Pause" while every automated entry was already held.
 * Merged into the badge on 2026-09-22; the badge reads the backend's decision
 * across all four mechanisms, so it is the one that knows most.
 *
 * Which half shows is `can_resume` — the backend's own answer to "would a
 * human pressing Resume change anything". Nothing is re-derived here; a second
 * opinion about a risk state produces two answers that drift.
 *
 * Resuming does more than clear a flag: the backend re-arms the post-close
 * guards, because otherwise a resume after a give-back halt lasts exactly
 * until the next trade closes. It goes through `resume-all`, not `resume`:
 * three separate mechanisms stop new orders and `resume` lifts only the first.
 *
 * The pause wording is the NiceGUI original's: what a pause does NOT stop is
 * the part people get wrong.
 */
export function TradingStatusDialog({
  open,
  onOpenChange,
  status,
  onChanged,
}: TradingStatusDialogProps) {
  const [hours, setHours] = useState("4");
  const [until, setUntil] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** Runs one write, and reports the failure instead of closing on it. */
  async function submit(request: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await request();
      onOpenChange(false);
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
      void submit(() => api.post("/api/trading/pause", { until: ts / 1000 }));
      return;
    }
    void submit(() => api.post("/api/trading/pause", { hours: Number(hours) || 4 }));
  }

  return (
    <DialogShell
      open={open}
      onOpenChange={onOpenChange}
      title={status.can_resume ? "Resume trading?" : "Pause trading"}
    >
      <div data-testid="trading-status-dialog" className="space-y-3 text-xs text-ink-2">
        <p className="font-semibold text-ink-1">{status.label}</p>
        {status.detail && <p>{status.detail}</p>}

        {status.can_resume ? (
          <p className="text-ink-3">
            Resuming lifts whatever is holding entries — a manual pause, a
            tripped circuit breaker, or today's profit target — and restarts
            the post-close guards' windows from now. Without that, resuming
            after a give-back halt would be undone by the next trade that
            closes.
          </p>
        ) : (
          <>
            <p>
              While paused, all signal generators and Telegram signals continue
              to run normally but no orders will be sent to MT5. Active trade
              management (SL/TP monitoring) continues as normal.
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                Pause for (hours)
                <Tooltip label="How long to stop sending new orders for, counted from now. Fractions are allowed — 0.25 is fifteen minutes. Trading resumes on its own when it expires.">
                  <input
                    aria-label="Pause for (hours)"
                    className="num mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
                    value={hours}
                    onChange={(e) => setHours(e.target.value)}
                  />
                </Tooltip>
                <span className="mt-0.5 block text-[10px] text-ink-3">
                  0.25 is fifteen minutes.
                </span>
              </label>
              <label className="block">
                Or until (YYYY-MM-DD HH:MM)
                <Tooltip label="A specific moment to pause until, which wins over the hours box beside it. A time already in the past is refused rather than stored — that would be a pause that never stops anything while this screen said it had.">
                  <input
                    aria-label="Or until (YYYY-MM-DD HH:MM)"
                    className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
                    value={until}
                    onChange={(e) => setUntil(e.target.value)}
                    placeholder="leave blank to use hours"
                  />
                </Tooltip>
              </label>
            </div>
          </>
        )}

        {error && <p role="alert" className="text-xs text-loss">{error}</p>}

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          {status.can_resume ? (
            <Button
              disabled={busy}
              // No body: resume-all re-reads the live state itself rather
              // than being told what to clear, because the click arrives
              // seconds after the poll that drew the badge.
              onClick={() => void submit(() => api.post("/api/trading/resume-all"))}
            >
              {busy ? "Resuming…" : "Resume Trading"}
            </Button>
          ) : (
            <Button disabled={busy} onClick={pause}>
              {busy ? "Pausing…" : "Pause now"}
            </Button>
          )}
        </div>
      </div>
    </DialogShell>
  );
}
