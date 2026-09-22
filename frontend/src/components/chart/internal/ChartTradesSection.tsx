import { EmptyState } from "@/components/shared/EmptyState";
import { formatLots, formatPrice } from "@/components/shared/format";
import type { Trade } from "@/api/types";

/**
 * The open positions drawn on the chart, as numbers.
 *
 * SL and TP live here rather than as lines on the candles — they were taken
 * off the chart on 2026-08-04 because they buried the price action.
 *
 * Every column but Side rendered an em dash until 2026-09-21: the rows are
 * `vantage_simulated_trades` columns (`lot_size`, `entry_price`, `stop_loss`,
 * `tp1`) and this reads the short names `ChartTrade` promises. The schema now
 * fills them — see `backend/src/api/schemas/chart.py`.
 *
 * **The gutters are load-bearing.** This sits in a pane the operator drags,
 * and the columns are seven numbers of similar shape. With no padding between
 * them a row renders as `BUY0.104320.444315.424324.42` the moment the pane is
 * narrower than the table wants — which is the same complaint the drag handle
 * was added to fix, arriving by a different route. So: a gutter on every cell,
 * no wrapping inside a price, and a sideways scroll when it still will not
 * fit. A number split across two lines is worse than one the eye has to
 * travel to.
 */
export function ChartTradesSection({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) {
    return (
      <EmptyState
        title="No open positions"
        hint="Positions opened by an engine or from the Trading tab appear here and on the chart."
      />
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full whitespace-nowrap text-xs">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
            <th className="py-1 pr-3 font-medium">Side</th>
            <th className="py-1 pr-3 font-medium">Lots</th>
            <th className="py-1 pr-3 font-medium">Entry</th>
            <th className="py-1 pr-3 font-medium">SL</th>
            <th className="py-1 pr-3 font-medium">TP</th>
            <th className="py-1 pr-3 font-medium">Source</th>
            <th className="py-1 font-medium">Ticket</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => (
            <tr key={String(t.id ?? i)} className="border-t border-line">
              <td className={t.direction === "SELL"
                ? "py-1 pr-3 text-loss" : "py-1 pr-3 text-profit"}>
                {String(t.direction ?? "—")}
              </td>
              <td className="num py-1 pr-3 text-ink-2">{formatLots(t.lots)}</td>
              <td className="num py-1 pr-3 text-ink-1">{formatPrice(t.entry)}</td>
              <td className="num py-1 pr-3 text-ink-3">{formatPrice(t.sl)}</td>
              <td className="num py-1 pr-3 text-ink-3">{formatPrice(t.tp)}</td>
              {/* Which engine or channel opened it, and the broker ticket to
                  match it against MT5. Both were already in the payload. */}
              <td className="py-1 pr-3 text-ink-2">
                {t.tg_source ? String(t.tg_source) : "—"}
              </td>
              <td className="num py-1 text-ink-3">
                {t.mt5_ticket ? String(t.mt5_ticket) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
