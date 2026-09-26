import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { Tooltip } from "@/components/shared/Tooltip";

/**
 * How long a successful switch's note stays in the title bar (owner,
 * 2026-09-26). Left there for good it took the space for as long as the app
 * ran. A refusal is not timed out: it can mean nothing is trading.
 */
export const OUTCOME_VISIBLE_MS = 5_000;

interface ActiveTraderControlProps {
  activeTrader: string;
  remoteConnected: boolean;
  onChanged: () => void;
}

/**
 * Which of the two paired machines is allowed to open new positions.
 *
 * **This is the only control in the shell that changes trading behaviour**, so
 * it asks first. Both nodes are pointed at the same MT5 account: a mis-click
 * that leaves two nodes active is two sets of engines trading one balance, and
 * nothing on either screen would say so.
 *
 * The confirmation is not boilerplate. It states what the backend is about to
 * do in order, because a handover that half-completes is the failure mode and
 * the operator is the one who will have to notice it. The result is shown
 * verbatim afterwards — "the VPS stood down; 2 of its positions keep running
 * to their own SL/TP" is a fact the operator needs and cannot get anywhere
 * else.
 *
 * Unpaired, the button is disabled with the reason on it rather than hidden:
 * "where did the Local/Remote switch go" is a worse question than "why can't I
 * press this".
 */
export function ActiveTraderControl(
  { activeTrader, remoteConnected, onChanged }: ActiveTraderControlProps,
) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null);

  const [vouched, setVouched] = useState(false);

  useEffect(() => {
    if (!outcome?.ok) return;
    const id = setTimeout(() => setOutcome(null), OUTCOME_VISIBLE_MS);
    return () => clearTimeout(id);
  }, [outcome]);
  const isLocal = activeTrader === "local";
  const target = isLocal ? "remote_vps" : "local";
  // REMOTE with the VPS unreachable: the ordinary take-over needs the VPS to
  // stand down and cannot happen, so this machine would stay not trading
  // indefinitely. The owner chose to allow taking over anyway, on the
  // operator's word that the VPS is not trading (2026-09-25).
  const forced = !isLocal && !remoteConnected;

  async function apply() {
    setBusy(true);
    try {
      const res = await api.put<{ note?: string }>(
        "/api/node/active-trader",
        forced ? { trader: target, without_peer: true } : { trader: target },
      );
      setOutcome({ ok: true, text: res.note ?? "Done." });
      onChanged();
      setAsking(false);
    } catch (e) {
      setOutcome({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
      setAsking(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        variant="ghost"
        onClick={() => { setOutcome(null); setVouched(false); setAsking(true); }}
        disabledReason={
          remoteConnected || forced
            ? undefined
            : "No remote node is connected. Pair one in Settings > Remote node."
        }
        title={
          isLocal
            ? "This machine is trading. Hand control back to the remote node."
            : "The remote node is trading. Take over on this machine."
        }
      >
        <span className={isLocal ? "text-warning" : "text-remote"}>
          {isLocal ? "LOCAL" : "REMOTE"}
        </span>
      </Button>

      <DialogShell
        open={asking}
        onOpenChange={setAsking}
        title={isLocal ? "Hand control back to the remote node?"
          : forced ? "Take over without the VPS?" : "Take over trading here?"}
      >
          <div className="space-y-3 text-xs text-ink-2">
            {forced ? (
              <>
                <p>
                  The VPS cannot be reached, so it cannot be asked to stand down.
                  This machine starts trading anyway. If the VPS is in fact still
                  running and trading, both will open trades on the same account.
                </p>
                <p className="text-ink-3">
                  When the VPS reconnects, it is told to stand down before
                  anything else.
                </p>
                <Tooltip label="Tick only once you have checked: the VPS is switched off, or MetaTrader 5 on it is closed. A VPS that is running and connected to the broker would trade the same account as this machine.">
                  <label className="flex items-start gap-2 text-ink-1">
                    <input type="checkbox" checked={vouched}
                      onChange={(e) => setVouched(e.target.checked)} />
                    The VPS is not trading: it is off, or MetaTrader 5 on it is closed.
                  </label>
                </Tooltip>
              </>
            ) : isLocal ? (
              <>
                <p>
                  This machine's engines stop first, then the remote node is asked
                  to resume. If it does not answer, the engines stay stopped and
                  nothing is trading until you sort it out.
                </p>
                <p className="text-ink-3">
                  Positions already open here keep being managed to their own SL
                  and TP. Handing back closes nothing.
                </p>
              </>
            ) : (
              <>
                <p>
                  The remote node is asked to stand down first, and only starts
                  this machine's engines once it has acknowledged. If it does not
                  answer, nothing changes and it stays the active trader.
                </p>
                <p className="text-ink-3">
                  Its open positions keep running to their own SL and TP.
                </p>
              </>
            )}
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" onClick={() => setAsking(false)}>Cancel</Button>
              <Button onClick={() => void apply()} disabled={busy || (forced && !vouched)}>
                {busy ? "Switching…" : isLocal ? "Hand back" : "Take over"}
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
