import { useCallback } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";

export interface CotSummary {
  report_date: string;
  mm_long: number;
  mm_short: number;
  mm_net: number;
  net_change: number | null;
  net_pct_oi: number | null;
  open_interest: number | null;
  cot_index: number;
  weeks: number;
  series: Array<{ date: string; net: number }>;
}

export interface GldSummary {
  date: string;
  tonnes: number;
  change_1d: number | null;
  change_5d: number | null;
  change_20d: number | null;
  series: Array<{ date: string; tonnes: number }>;
}

export interface Positioning {
  cot: CotSummary | null;
  cot_fetched_at: number | null;
  cot_error: string | null;
  gld: GldSummary | null;
  gld_fetched_at: number | null;
  gld_error: string | null;
}

/** COT and GLD (docs/todo/024). Weekly and daily data: a slow poll is plenty,
 *  and the backend caches each source for hours anyway. */
export function usePositioning() {
  return usePoll<Positioning>(
    "news/positioning",
    useCallback(() => api.get<Positioning>("/api/news/positioning"), []),
    600_000,
  );
}
