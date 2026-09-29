import { useState } from "react";
import { RotateCw } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";

/**
 * Restart the MT5 bridge from Settings (owner, 2026-09-29). Until then the
 * only way was Telegram's /restart_bridge -- and after an update that changes
 * mt5_bridge.py, the Mac keeps serving the old code until the bridge restarts
 * ("MT5 bridge already listening -- leaving it alone").
 *
 * On a Mac the restart tears down the whole Wine session, so MetaTrader and
 * the EA go with it. Nothing is closed at the broker, but for those seconds
 * nothing manages an open position. So one press only asks; the confirm sends.
 * The request waits for the bridge to reconnect and answers in its own words.
 */
export function BridgeRestartSection({ platform }: { platform: string }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);
  const mac = platform === "darwin";

  async function restart() {
    setBusy(true);
    setResult(null);
    try {
      const res = await api.post<{ ok: boolean; message: string }>(
        "/api/settings/mt5/restart-bridge", {});
      setResult(res);
      setOpen(false);
    } catch (e) {
      setResult({ ok: false, message: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section data-testid="bridge-restart" className="rounded border border-line p-3">
      <h3 className="text-xs font-semibold text-ink-1">Restart the bridge</h3>
      <p className="mb-2 text-[11px] text-ink-3">
        Use after an update, or when the header says the bridge is disconnected
        and it does not come back on its own.
      </p>
      <Button variant="ghost" disabled={busy}
        onClick={() => { setResult(null); setOpen(true); }}>
        <RotateCw size={13} /> {busy ? "Restarting…" : "Restart bridge"}
      </Button>
      {result && (
        <p role={result.ok ? "status" : "alert"}
          className={`mt-1 text-[11px] ${result.ok ? "text-profit" : "text-loss"}`}>
          {result.message}
        </p>
      )}

      <DialogShell open={open} onOpenChange={setOpen} title="Restart the MT5 bridge">
        <div className="space-y-3 text-xs text-ink-2">
          {mac ? (
            <p>
              MetaTrader and the EA inside it close and reopen with the bridge,
              which takes about 30 seconds. Nothing is closed at the broker and
              positions keep their SL/TP, but nothing manages them until it is
              back. The EA takes its trades back when it reconnects.
            </p>
          ) : (
            <p>
              The app reconnects to MetaTrader in place; the terminal and the EA
              keep running. A changed bridge file loads on the next app restart.
            </p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button disabled={busy} onClick={() => void restart()}>
              <RotateCw size={13} /> {busy ? "Restarting the bridge…" : "Restart the bridge"}
            </Button>
          </div>
        </div>
      </DialogShell>
    </section>
  );
}
