import { useCallback, useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { DialogShell } from "@/components/shared/DialogShell";
import { Button } from "@/components/shared/Button";
import { Notice } from "@/components/shared/Notice";
import { Tooltip } from "@/components/shared/Tooltip";
import { AccountBadge } from "./AccountBadge";

interface Account {
  login: string;
  server: string;
  configured: boolean;
  /** Saved, but the key cannot decrypt it. A different problem from absent,
   *  and the advice for the two is opposite. */
  unreadable?: boolean;
}
interface EnvState {
  current: string;
  environments: Record<string, Account>;
}

interface EnvironmentControlProps {
  /** The bridge's account, so the badge shows ground truth rather than config. */
  account?: Record<string, unknown> | null;
}

/** How often the configured environment is re-read. */
const POLL_MS = 15_000;

const NAME: Record<string, string> = { demo: "Demo", live: "Live" };

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
 * Switching to live asks first, and the question names the account — the login
 * and the server — because "are you sure?" is a question people learn to click
 * through and "switch to 900123 on Vantage-Live?" is one they read. Switching
 * back to demo is the safe direction and is asked more quietly: dressing it up
 * the same way would train the habit this guard exists to prevent.
 *
 * **The badge and the switch answer to different sources, and they can
 * disagree.** The badge shows what the BRIDGE says is connected; the direction
 * of the switch comes from what the APP is configured for. On 2026-09-21 an
 * install had `account_env: live` while MetaTrader was still logged into demo:
 * the badge read DEMO, the owner pressed it expecting live, and the control
 * correctly switched to demo — which looked exactly like doing nothing. That
 * disagreement is the half-switched state `services/broker/environment.py`
 * exists to prevent, so it is now said out loud, and the dialog names the
 * account being LEFT as well as the one being taken.
 *
 * The state is re-read on a poll rather than once on mount, because it changes
 * under this component: a switch, or the other paired node taking over, leaves
 * a button offering the direction that was right ten minutes ago.
 */
export function EnvironmentControl({ account = null }: EnvironmentControlProps) {
  const [state, setState] = useState<EnvState | null>(null);
  const [asking, setAsking] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null);

  const read = useCallback(async () => {
    try {
      setState(await api.get<EnvState>("/api/settings/environment"));
    } catch {
      // A header control that cannot read its own state renders nothing
      // rather than a wrong answer. "DEMO" on a live account is the one
      // outcome worth avoiding at any cost.
      setState(null);
    }
  }, []);

  useEffect(() => {
    void read();
    const id = setInterval(() => void read(), POLL_MS);
    return () => clearInterval(id);
  }, [read]);

  const dismiss = useCallback(() => setOutcome(null), []);

  if (!state) return null;

  const live = state.current === "live";
  const target = live ? "demo" : "live";
  const targetAccount = state.environments?.[target];

  // What the bridge says, against what this app is configured for. `is_demo`
  // is only an opinion when the bridge has answered at all.
  const bridgeIsDemo = account?.["is_demo"];
  const mismatch =
    (bridgeIsDemo === true && live) || (bridgeIsDemo === false && !live);

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
      void read();
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
          targetAccount?.unreadable
            // NOT "add it under Settings > MT5". The credentials ARE saved;
            // they cannot be decrypted, and re-typing them will not fix that.
            // Seen live on 2026-09-19 when the app ran under an interpreter
            // with no `keyring` and every password decrypted to "".
            ? `The ${target} account (${targetAccount.login}) is saved but its `
              + "password cannot be decrypted on this machine. Start the app "
              + "with the project's own .venv, which has the keychain library."
            : !targetAccount?.configured
              ? `No ${target} account is configured. Add it under Settings > MT5.`
              // The direction, in the words of the CONFIG, not the badge. The
              // badge can be showing the other account -- see the mismatch
              // marker beside it.
              : `This app is configured for the ${NAME[state.current]} account. `
                + `Click to switch it to ${NAME[target]}.`
        }
        className="rounded transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-60"
      >
        <AccountBadge account={account} configured={state.current} />
      </button>

      {mismatch && (
        <Tooltip
          label={
            `This app is configured for the ${NAME[state.current]} account, but `
            + `MetaTrader 5 is logged into the ${NAME[live ? "demo" : "live"]} one. `
            + "Log MT5 into the configured account, or switch the app to match it. "
            + "Until they agree, the badge and the switch are describing different "
            + "accounts."
          }
        >
          <span
            data-testid="environment-mismatch"
            title={
              `This app is configured for the ${NAME[state.current]} account, but `
              + `MetaTrader 5 is logged into the ${NAME[live ? "demo" : "live"]} one.`
            }
            className="flex shrink-0 items-center gap-1 rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-[10px] font-semibold text-warning"
          >
            <TriangleAlert size={11} aria-hidden />
            <span className="hidden lg:inline">MT5 ≠ {NAME[state.current]}</span>
          </span>
        </Tooltip>
      )}

      <DialogShell
        open={asking !== null}
        onOpenChange={(v) => setAsking(v ? target : null)}
        title={target === "live" ? "Switch to the LIVE account?" : "Switch back to demo?"}
      >
        <div data-testid="environment-dialog" className="space-y-3 text-xs text-ink-2">
          {target === "live" ? (
            <p className="rounded border border-loss/40 bg-loss/10 px-2 py-1.5 text-loss">
              This points every engine, every order and every number in this app
              at <strong>real money</strong>.
            </p>
          ) : (
            <p>Trading, history and every number go back to the demo account.</p>
          )}

          {/* Both sides, always. "Switch to demo" on a badge reading DEMO is
              the sentence that made this look like it had done nothing. */}
          <p className="text-ink-3">
            Away from the <strong>{NAME[state.current]}</strong> account this app
            is configured for, to account{" "}
            <span className="num text-ink-2">{targetAccount?.login}</span> on{" "}
            <span className="num text-ink-2">{targetAccount?.server}</span>. Make sure
            MetaTrader 5 is logged into that account.
          </p>
          {mismatch && (
            <p className="rounded border border-warning/40 bg-warning/10 px-2 py-1.5 text-warning">
              MetaTrader 5 is currently logged into the{" "}
              {NAME[live ? "demo" : "live"]} account, which is not the one this
              app is configured for. Check which account you mean before
              switching.
            </p>
          )}
          <p className="text-ink-3">
            The app restarts to apply it — every cached handle was built against
            the account it is leaving. This page reloads itself when it comes
            back.
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
        <Notice tone={outcome.ok ? "ok" : "bad"} onDismiss={dismiss}>
          {outcome.text}
        </Notice>
      )}
    </>
  );
}
