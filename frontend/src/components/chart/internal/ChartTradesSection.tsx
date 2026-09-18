import { EmptyState } from "@/components/shared/EmptyState";
import { formatLots, formatPrice } from "@/components/shared/format";
import type { Trade } from "@/api/types";

/**
 * The open positions drawn on the chart, as numbers.
 *
 * SL and TP live here rather than as lines on the candles — they were taken
 * off the chart on 2026-08-04 because they buried the price action.
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
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
          <th className="py-1 font-medium">Side</th>
          <th className="py-1 font-medium">Lots</th>
          <th className="py-1 font-medium">Entry</th>
          <th className="py-1 font-medium">SL</th>
          <th className="py-1 font-medium">TP</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t, i) => (
          <tr key={String(t.id ?? i)} className="border-t border-line">
            <td className={t.direction === "SELL" ? "py-1 text-loss" : "py-1 text-profit"}>
              {String(t.direction ?? "—")}
            </td>
            <td className="num py-1 text-ink-2">{formatLots(t.lots)}</td>
            <td className="num py-1 text-ink-1">{formatPrice(t.entry)}</td>
            <td className="num py-1 text-ink-3">{formatPrice(t.sl)}</td>
            <td className="num py-1 text-ink-3">{formatPrice(t.tp)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
