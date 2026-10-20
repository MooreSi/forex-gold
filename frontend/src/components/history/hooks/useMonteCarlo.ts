import { useCallback } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";

export interface BandPoint { n: number; p5: number; p50: number; p95: number }

export interface MonteCarlo {
  trades: number;
  horizon?: number;
  balance?: number;
  paths?: number;
  /** Set instead of the numbers when there is nothing honest to show. */
  note?: string | null;
  mean_trade?: number;
  final?: Record<"p5" | "p25" | "p50" | "p75" | "p95", number>;
  max_dd_pct?: Record<"p50" | "p95" | "p99", number>;
  prob_dd?: Record<"10" | "20" | "30" | "50", number>;
  prob_loss?: number;
  prob_ruin?: number;
  band?: BandPoint[];
}

/**
 * The window's Monte Carlo (docs/todo/024). Its own endpoint and a slow poll:
 * 5,000 resampled paths is work the trade table's 15-second poll should not
 * pay for, and the answer only moves when a trade closes.
 */
export function useMonteCarlo(days: number, horizon: number | null) {
  const query = horizon ? `days=${days}&horizon=${horizon}` : `days=${days}`;
  return usePoll<MonteCarlo>(
    `history/monte-carlo/${query}`,
    useCallback(() => api.get<MonteCarlo>(`/api/history/monte-carlo?${query}`), [query]),
    300_000,
  );
}
