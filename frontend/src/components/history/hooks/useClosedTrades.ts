import { useCallback } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";

export interface TradeRow {
  ticket: number;
  direction: string;
  entry_price: number;
  exit_price: number;
  open_ts: number;
  close_ts: number;
  lots: number;
  close_lots: number[];
  pnl: number;
  fees: number;
  pips: number | null;
  duration_secs: number | null;
  order_type: string;
  pending_secs: number | null;
  reason: string;
  source: string;
  strategy: string;
  max_tp: string;
  rr: number | null;
  spread_points: number | null;
  group: [string, number] | null;
}

export interface CurvePoint { ts: number; pnl: number }

export interface ClosedTrades {
  rows: TradeRow[];
  error: string | null;
  curve: {
    points: CurvePoint[];
    net: number;
    peak: number;
    max_drawdown: number;
    trades: number;
  };
}

/**
 * The window's closed trades, and the curve drawn from them, in one read.
 *
 * Two panels show this — the equity curve and the trade table — and they
 * share one request because they share the poll key. That is the whole point
 * of `usePoll`'s registry: a second key here would mean two fetches of a row
 * per trade on every interval, and two chances for the curve and the table
 * under it to disagree about a moving account.
 */
export function useClosedTrades(days: number) {
  return usePoll<ClosedTrades>(
    `history/trades/${days}`,
    useCallback(() => api.get<ClosedTrades>(`/api/history/trades?days=${days}`), [days]),
    60_000,
  );
}
