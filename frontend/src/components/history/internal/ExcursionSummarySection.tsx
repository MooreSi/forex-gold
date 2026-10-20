import { StatCard } from "@/components/shared/StatCard";
import { asArray } from "@/lib/asArray";
import { useClosedTrades, type TradeRow } from "../hooks/useClosedTrades";
import { median } from "./excursionFormat";

/**
 * What the window's excursions say, in three numbers (docs/todo/024).
 *
 * The questions MAE/MFE answer for a stop and a target: how much heat do
 * winners take before they win (if it is far less than the stop, the stop is
 * wider than it needs to be); how far did losers go our way first (if far,
 * a breakeven move or a nearer target would have saved them); and how much of
 * the best price did winners keep. Shares the trade table's poll key, so it
 * costs no extra request.
 */
export function ExcursionSummarySection({ days }: { days: number }) {
  const rows = asArray<TradeRow>(useClosedTrades(days).data?.rows);
  const measured = rows.filter((r) => r.mae_pips != null && r.mfe_pips != null);
  if (measured.length === 0) return null;

  const winners = measured.filter((r) => r.pnl > 0);
  const losers = measured.filter((r) => r.pnl <= 0);
  const winHeat = median(winners.map((r) => r.mae_pips as number));
  const loserRun = median(losers.map((r) => r.mfe_pips as number));
  const kept = median(winners.filter((r) => r.capture != null).map((r) => r.capture as number));
  const pips = (v: number | null) => (v == null ? "—" : `${v.toFixed(1)} pips`);

  return (
    <div className="mb-3 grid gap-2 sm:grid-cols-4" data-testid="excursion-summary">
      <StatCard label="Winners' heat (median MAE)" value={pips(winHeat)}
        hint="How far a winning trade typically went against you before it won. Much smaller than your stop means the stop is wider than winners need." />
      <StatCard label="Losers' best (median MFE)" value={pips(loserRun)}
        hint="How far a losing trade typically went your way before it lost. A large figure means a breakeven move or a nearer first target would have saved some." />
      <StatCard label="Winners kept (median capture)"
        value={kept == null ? "—" : `${Math.round(kept * 100)}%`}
        hint="Pips banked as a share of the best price the trade reached." />
      <StatCard label="Measured" value={`${measured.length} of ${rows.length}`}
        hint="Trades with candle history to measure. The rest are filled in on later refreshes, or are older than the history." />
    </div>
  );
}
