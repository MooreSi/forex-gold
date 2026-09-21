import { useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatLots, formatPrice, formatSignedMoney, pnlColour } from "@/components/shared/format";
import type { Trade } from "@/api/types";

interface ActiveTradesSectionProps {
  trades: Trade[];
  disabledReason: string | null;
  onChanged: () => void;
}

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

  return (
    <>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
            <th className="py-1 font-medium">Side</th>
            <th className="py-1 font-medium">Lots</th>
            <th className="py-1 font-medium">Entry</th>
            <th className="py-1 font-medium">SL</th>
            <th className="py-1 font-medium">P&amp;L</th>
            <th className="py-1 font-medium">Strategy</th>
            <th className="py-1 font-medium">Source</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => {
            const pnl = typeof t["pnl"] === "number" ? (t["pnl"] as number) : null;
            return (
              <tr key={String(t.id ?? i)} className="border-t border-line">
                <td className={t.direction === "SELL" ? "py-1.5 text-loss" : "py-1.5 text-profit"}>
                  {String(t.direction ?? "—")}
                </td>
                <td className="num py-1.5 text-ink-2">{formatLots(t.lots)}</td>
                <td className="num py-1.5 text-ink-1">{formatPrice(t.entry)}</td>
                <td className="num py-1.5 text-ink-3">{formatPrice(t.sl)}</td>
                <td className={`num py-1.5 ${pnlColour(pnl)}`}>{formatSignedMoney(pnl)}</td>
                {/* Both decided by the backend: `strategy` is stored as
                    `template:<name>` and `tg_source` holds an engine name, a
                    channel name or a marker. Re-deriving that here would be a
                    second answer to what a trade's source is. */}
                <td className="py-1.5 text-ink-2">
                  {t.strategy_label ? String(t.strategy_label) : "—"}
                </td>
                <td className="py-1.5 text-ink-2">
                  {t.source_label ? String(t.source_label) : "—"}
                </td>
                <td className="py-1.5 text-right">
                  <Button
                    variant="danger"
                    onClick={() => { setRefusal(null); setClosing(t); }}
                    disabledReason={disabledReason}
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
