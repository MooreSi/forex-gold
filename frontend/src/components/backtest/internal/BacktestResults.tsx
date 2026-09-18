import { TriangleAlert } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import {
  formatMoney, formatPercent, formatSignedMoney, pnlColour,
} from "@/components/shared/format";
import type { BacktestResult } from "@/api/types";

const FILTER_REASONS: [string, string][] = [
  ["out_of_window", "outside the candle window"],
  ["zero_sl", "no stop loss"],
  ["point_entry", "no entry zone"],
  ["wide_sl", "stop too wide"],
  ["bad_tp", "unusable targets"],
];

function n(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

/**
 * The comparison table, plus why signals were dropped before the walk.
 *
 * A strategy the walk refused renders its reason across the row instead of its
 * zeros. Zeros beside a row showing a real drawdown read as an argument for
 * the strategy that was never tested, which is the one way this screen can
 * actively mislead.
 */
export function BacktestResults({ result }: { result: BacktestResult }) {
  if (result.note) {
    return <EmptyState title="Nothing to walk" hint={result.note} />;
  }
  if (result.results.length === 0) {
    return <EmptyState title="The walk produced no rows" />;
  }

  const filtered = result.filtered;
  const dropped = FILTER_REASONS.filter(([key]) => (n(filtered[key]) ?? 0) > 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-3">
        <span>
          <span className="num text-ink-2">{n(filtered["valid"]) ?? 0}</span> of{" "}
          <span className="num text-ink-2">{n(filtered["total"]) ?? 0}</span> signals walked
        </span>
        <span>
          <span className="num text-ink-2">{result.candles_loaded}</span>{" "}
          {result.granularity === "ticks" ? "candles for the tick window" : "candles"}
        </span>
        {dropped.map(([key, label]) => (
          <span key={key}>
            <span className="num text-warning">{n(filtered[key])}</span> {label}
          </span>
        ))}
      </div>

      {String(filtered["candle_start"] ?? "") && (
        <p className="text-[11px] text-ink-3">
          candles {String(filtered["candle_start"])} → {String(filtered["candle_end"])} ·
          signals {String(filtered["signal_start"])} → {String(filtered["signal_end"])}
        </p>
      )}

      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
            <th className="py-1 font-medium">Strategy</th>
            <th className="py-1 font-medium">Trades</th>
            <th className="py-1 font-medium">Win rate</th>
            <th className="py-1 font-medium">P&amp;L</th>
            <th className="py-1 font-medium">Profit factor</th>
            <th className="py-1 font-medium">Max DD</th>
            <th className="py-1 font-medium">Final</th>
          </tr>
        </thead>
        <tbody>
          {result.results.map((row) =>
            row.unsupported_reason ? (
              <tr key={row.strategy} className="border-t border-line">
                <td className="py-1.5 text-ink-2">{row.strategy}</td>
                <td colSpan={6} className="py-1.5">
                  <span
                    data-testid={`unsupported-${row.strategy}`}
                    className="flex items-center gap-1.5 text-warning"
                  >
                    <TriangleAlert size={12} />
                    not simulated: {row.unsupported_reason}
                  </span>
                </td>
              </tr>
            ) : (
              <tr key={row.strategy} className="border-t border-line">
                <td className="py-1.5 text-ink-1">{row.strategy}</td>
                <td className="num py-1.5 text-ink-2">{row.trades}</td>
                <td className="num py-1.5 text-ink-2">{formatPercent(row.win_rate)}</td>
                <td className={`num py-1.5 ${pnlColour(row.total_pnl)}`}>
                  {formatSignedMoney(row.total_pnl)}
                </td>
                <td className="num py-1.5 text-ink-2">{row.profit_factor.toFixed(2)}</td>
                <td className="num py-1.5 text-loss">{formatPercent(row.max_drawdown_pct)}</td>
                <td className="num py-1.5 text-ink-1">{formatMoney(row.final_balance)}</td>
              </tr>
            ),
          )}
        </tbody>
      </table>
    </div>
  );
}
