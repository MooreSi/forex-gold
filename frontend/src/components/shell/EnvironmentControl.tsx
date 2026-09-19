import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import { DialogShell } from "@/components/shared/DialogShell";
import { Button } from "@/components/shared/Button";
import { AccountBadge } from "./AccountBadge";

interface Account { login: string; server: string; configured: boolean }
interface EnvState {
  current: string;
  environments: Record<string, Account>;
}

interface EnvironmentControlProps {
  /** The bridge's account, so the badge shows ground truth rather than config. */
  account?: Record<string, unknown> | null;
}

/**
 * Demo or live: which account the whole app is pointed at.
 *
 * **The badge IS the switch.** Until 2026-09-19 the header carried a green
 * "DEMO 5203117" box and, next to it, a separate ghost button also reading
 * "DEMO" — the same fact twice, and neither of them obviously the control.
 * Now the box is the button.
 *
 * **The biggest single control in this dashboard.** Every other setting decides
 * what happens on whichever account is selected; this decides whether that
 * account holds real money.
 *
 * Switching to live asks twice, and the second ask names the account — the
 * login and the server — because "are you sure?" is a question people learn to
 * click through and "switch to 900123 on Vantage-Live?" is one they read.
 * Switching back to demo is the safe direction and does not: asking for that
 * too would train the habit this guard exists to prevent.
 *
 * The app restarts afterwards, which the dialog says before it happens. Every
 * cached handle — the runtime, the bridge, the engines — was built against the
 * old account.
 */
export function EnvironmentControl({ account = null }: EnvironmentControlProps) {
  const [state, setState] = useState<EnvState | null>(null);
  const [asking, setAsking] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setState(await api.get<EnvState>("/api/settings/environment"));
      } catch {
        // A header control that cannot read its own state renders nothing
        // rather than a wrong answer. "DEMO" on a live account is the one
        // outcome worth avoiding at any cost.
        setState(null);
      }
    })();
  }, []);

  if (!state) return null;

  const live = state.current === "live";
  const target = live ? "demo" : "live";
  const targetAccount = state.environments?.[target];

  async function apply() {
    setBusy(true);
    try {
      const res = await api.put<{ note?: string; restart?: string }>(
        "/api/settings/environment", { environment: target, confirm: true },
      );
      setOutcome({ ok: true, text: [res.note, res.restart].filter(Boolean).join(" ") });
      setAsking(null);
    } catch (e) {
      setOutcome({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
      setAsking(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        data-testid="environment-control"
        onClick={() => { setOutcome(null); setAsking(target); }}
        disabled={!targetAccount?.configured}
        title={
          !targetAccount?.configured
            ? `No ${target} account is configured. Add it under Settings > MT5.`
            : live
              ? "This app is pointed at the LIVE account. Click to switch back to demo."
              : "This app is pointed at the demo account. Click to switch to live."
        }
        className="rounded transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-60"
      >
        <AccountBadge account={account} configured={state.current} />
      </button>

      <DialogShell
        open={asking !== null}
        onOpenChange={(v) => setAsking(v ? target : null)}
        title={target === "live" ? "Switch to the LIVE account?" : "Switch back to demo?"}
      >
        <div className="space-y-3 text-xs text-ink-2">
          {target === "live" ? (
            <p className="rounded border border-loss/40 bg-loss/10 px-2 py-1.5 text-loss">
              This points every engine, every order and every number in this app
              at <strong>real money</strong>.
            </p>
          ) : (
            <p>Trading, history and every number go back to the demo account.</p>
          )}

          <p className="text-ink-3">
            Account <span className="num text-ink-2">{targetAccount?.login}</span> on{" "}
            <span className="num text-ink-2">{targetAccount?.server}</span>. Make sure
            MetaTrader 5 is logged into that account.
          </p>
          <p className="text-ink-3">
            The app restarts to apply it — every cached handle was built against
            the account it is leaving.
          </p>

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setAsking(null)}>Cancel</Button>
            <Button disabled={busy} onClick={() => void apply()}>
              {busy
                ? "Switching…"
                : target === "live" ? "Switch to LIVE" : "Switch to demo"}
            </Button>
          </div>
        </div>
      </DialogShell>

      {outcome && (
        <span
          role={outcome.ok ? "status" : "alert"}
          className={`max-w-sm text-[11px] ${outcome.ok ? "text-profit" : "text-loss"}`}
        >
          {outcome.text}
        </span>
      )}
    </>
  );
}
