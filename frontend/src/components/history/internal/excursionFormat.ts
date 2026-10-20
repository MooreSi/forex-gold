import type { TradeRow } from "../hooks/useClosedTrades";

/**
 * Cell text for the excursion columns (docs/todo/024).
 *
 * Three states, kept apart on purpose: a number; "n/a" when the backend tried
 * and could not measure the trade (its bars and its prices disagree, or it is
 * older than the candle history); and an em dash when nothing has looked yet.
 * A zero in place of either would read as a trade that never moved.
 */
export function excursionText(row: TradeRow, side: "mae" | "mfe"): string {
  const pips = side === "mae" ? row.mae_pips : row.mfe_pips;
  if (pips == null) return row.excursion_res === "none" ? "n/a" : "—";
  return `${side === "mae" ? "-" : "+"}${pips.toFixed(1)}`;
}

export function excursionTitle(row: TradeRow): string | undefined {
  if (row.excursion_res === "none") {
    return "Could not be measured: the candles and this trade's prices disagree, or it is older than the candle history.";
  }
  if (row.mae_usd == null || row.mfe_usd == null) return undefined;
  return `Against: $${row.mae_usd.toFixed(2)}, for: $${row.mfe_usd.toFixed(2)} at the opening lots, from ${row.excursion_res} bars. Bars are bid prices, so a SELL's adverse figure is understated by the spread.`;
}

export function captureText(row: TradeRow): string {
  if (row.capture == null) return row.excursion_res === "none" ? "n/a" : "—";
  return `${Math.round(row.capture * 100)}%`;
}

export function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
