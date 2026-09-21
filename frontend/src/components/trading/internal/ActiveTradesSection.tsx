import { useState } from "react";
import { TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { EmptyState } from "@/components/shared/EmptyState";
import { Tooltip } from "@/components/shared/Tooltip";
import { formatLots, formatPrice, formatSignedMoney, pnlColour } from "@/components/shared/format";
import type { Trade } from "@/api/types";

interface ActiveTradesSectionProps {
  trades: Trade[];
  disabledReason: string | null;
  onChanged: () => void;
}

/** Why an untracked position has no Close button here. */
const UNTRACKED_REASON =
  "This position is open at the broker but this app has no record of it, so "
  + "there is nothing to close against and nothing to update afterwards. "
  + "Close it in MetaTrader 5.";

/**
 * Open positions, and the control that closes one.
 *
 * Closing is money-touching, so it goes through the same confirmation bar as
 * opening: the dialog names the position it is about to close, and nothing is
 * a single click.
 *
 * Every column but Side rendered an em dash until 2026-09-21 -- the rows are
 * raw `vantage_simulated_trades` columns and this reads the short names. The
 * same gap made `trade.id` undefined, so the Close button's URL was
 * `/api/trading/trades/undefined/close`. Both fixed by giving the route the
 * `ChartTrade` model the chart's own trades already used.
 *
 * Two more from the same day's report, both answered in the backend and
 * rendered here:
 *
 * * **P&L is the broker's RUNNING number**, from its live positions payload.
 *   `mt5_profit` is only written when a trade closes, so this column was an em
 *   dash on every open row. It refreshes with the table's poll.
 * * **A position with no record still appears**, flagged, because "it is
 *   showing one position when on mt5 there are two" is the table being
 *   confidently wrong rather than incomplete. It cannot be closed from here --
 *   see `UNTRACKED_REASON`.
 */
export function ActiveTradesSection({
  trades, disabledReason, onChanged,
}: ActiveTradesSectionProps) {
  const [closing, setClosing] = useState<Trade | null>(null);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);

  const confirmClose = async () => {
    if (!closing) return;
    setBusy(true);
    setRefusal(null);
    try {
      await api.post(`/api/trading/trades/${String(closing.id)}/close`, {});
      setClosing(null);
      onChanged();
    } catch (e) {
      setRefusal(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (trades.length === 0) {
    return (
      <EmptyState
        title="No open positions"
        hint="An engine or a manual order will put one here."
      />
    );
  }

  const untrackedCount = trades.filter((t) => t.untracked).length;

  return (
    <>
      {untrackedCount > 0 && (
        // Said once at the top as well as marked per row. A flag on a row is
        // easy to read past; the count is the thing that answers "MT5 says two
        // and this says one".
        <p
          data-testid="untracked-summary"
          role="status"
          className="mb-2 flex items-center gap-1.5 rounded border border-warning/40 bg-warning/10 px-2 py-1 text-[11px] text-warning"
        >
          <TriangleAlert size={12} aria-hidden />
          {untrackedCount} position{untrackedCount === 1 ? " is" : "s are"} open at
          the broker that this app has no record of. {UNTRACKED_REASON}
        </p>
      )}

      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
            <Th help="Buy or sell.">Side</Th>
            <Th help="Position size in lots.">Lots</Th>
            <Th help="The price the position was filled at.">Entry</Th>
            <Th help="Where the broker will close it for a loss. A dash means no stop is set.">SL</Th>
            <Th help="The first take-profit. A position can carry up to eight; the rest are on the chart.">TP</Th>
            <Th help="The broker's running profit or loss on this position, including swap. It updates with the table.">
              P&amp;L
            </Th>
            <Th help="The template or strategy the position was opened under.">Strategy</Th>
            <Th help="The engine, channel or manual action that produced it.">Source</Th>
            <Th help="The broker's own ticket, for matching the row against MetaTrader 5.">Ticket</Th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => {
            const pnl = typeof t["pnl"] === "number" ? (t["pnl"] as number) : null;
            const untracked = t.untracked === true;
            return (
              <tr
                key={String(t.id ?? t.mt5_ticket ?? i)}
                data-testid={untracked ? "position-row-untracked" : "position-row"}
                className={untracked ? "border-t border-warning/40 bg-warning/5" : "border-t border-line"}
              >
                <td className={t.direction === "SELL" ? "py-1.5 text-loss" : "py-1.5 text-profit"}>
                  {String(t.direction ?? "—")}
                </td>
                <td className="num py-1.5 text-ink-2">{formatLots(t.lots)}</td>
                <td className="num py-1.5 text-ink-1">{formatPrice(t.entry)}</td>
                <td className="num py-1.5 text-ink-3">{formatPrice(t.sl)}</td>
                <td className="num py-1.5 text-ink-3">{formatPrice(t.tp)}</td>
                <td data-testid="position-pnl" className={`num py-1.5 ${pnlColour(pnl)}`}>
                  {formatSignedMoney(pnl)}
                </td>
                {/* Both decided by the backend: `strategy` is stored as
                    `template:<name>` and `tg_source` holds an engine name, a
                    channel name or a marker. Re-deriving that here would be a
                    second answer to what a trade's source is. */}
                <td className="py-1.5 text-ink-2">
                  {t.strategy_label ? String(t.strategy_label) : "—"}
                </td>
                <td className="py-1.5 text-ink-2">
                  {untracked ? (
                    <Tooltip label={UNTRACKED_REASON}>
                      <span className="inline-flex items-center gap-1 text-warning">
                        <TriangleAlert size={11} aria-hidden />
                        {t.source_label ? String(t.source_label) : "not tracked"}
                      </span>
                    </Tooltip>
                  ) : (
                    t.source_label ? String(t.source_label) : "—"
                  )}
                </td>
                <td className="num py-1.5 text-ink-3">
                  {t.mt5_ticket == null ? "—" : String(t.mt5_ticket)}
                </td>
                <td className="py-1.5 text-right">
                  <Button
                    variant="danger"
                    onClick={() => { setRefusal(null); setClosing(t); }}
                    disabledReason={untracked ? UNTRACKED_REASON : disabledReason}
                  >
                    Close
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <DialogShell
        open={closing !== null}
        onOpenChange={(o) => !o && setClosing(null)}
        title="Close this position"
        footer={
          <>
            <Button variant="ghost" onClick={() => setClosing(null)} disabled={busy}>
              Keep it open
            </Button>
            <Button variant="danger" onClick={() => void confirmClose()} disabled={busy}>
              {busy ? "Closing…" : "Close it"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-1">
          Close {String(closing?.direction ?? "")} XAUUSD, {formatLots(closing?.lots)} lots,
          opened at {formatPrice(closing?.entry)}.
        </p>
        <p className="mt-2 text-xs text-ink-3">
          This closes the position at the current market price.
        </p>
        {refusal && (
          <p role="alert" className="mt-3 rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            {refusal}
          </p>
        )}
      </DialogShell>
    </>
  );
}

/**
 * A column heading that explains itself on hover.
 *
 * The tooltip wraps the SPAN inside the cell, not the cell: `<tr>` may only
 * contain `<th>` and `<td>`, and React says so out loud.
 */
function Th({ help, children }: { help: string; children: React.ReactNode }) {
  return (
    <th className="py-1 font-medium">
      <Tooltip label={help} side="bottom">
        <span className="cursor-help">{children}</span>
      </Tooltip>
    </th>
  );
}
