import { useCallback, useMemo, useState } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import type { Candle, Overlays, Tick, Trade } from "@/api/types";

export const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1H", "4H", "1D"] as const;
export type Timeframe = (typeof TIMEFRAMES)[number];

/** Seconds per bar, for moving the forming bar with the tick. */
export const TIMEFRAME_SECONDS: Record<Timeframe, number> = {
  "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1H": 3600, "4H": 14_400, "1D": 86_400,
};

/**
 * All of the Chart tab's state and fetching, in one place, so `ChartPanel`
 * stays composition.
 *
 * Three polls, deliberately: the tick moves every second, candles and their
 * overlays move once a bar, and open trades change only when something happens. One interval
 * for all three would either hammer the bridge for candles or show a price
 * that lags ten seconds behind the market.
 *
 * Candles and overlays share a key suffix so they always describe the same
 * window; asking for them separately at different counts is how an EMA ends up
 * drawn against the wrong bars.
 */
export function useChartController() {
  const [timeframe, setTimeframe] = useState<Timeframe>("5m");
  const [count, setCount] = useState(200);
  const window = `${timeframe}&count=${count}`;

  const candles = usePoll<Candle[]>(
    `chart/candles?${window}`,
    useCallback(
      () => api.get<Candle[]>(`/api/chart/candles?timeframe=${timeframe}&count=${count}`),
      [timeframe, count],
    ),
    10_000,
  );

  const overlays = usePoll<Overlays>(
    `chart/overlays?${window}`,
    useCallback(
      () => api.get<Overlays>(`/api/chart/overlays?timeframe=${timeframe}&count=${count}`),
      [timeframe, count],
    ),
    10_000,
  );

  const tick = usePoll<Tick | null>(
    "chart/tick",
    useCallback(() => api.get<Tick | null>("/api/chart/tick"), []),
    // Every second since 2026-09-28 ("as close to real time as possible"): the
    // tick also moves the forming candle. Cheap: the server caches the tick
    // for 1 s (TICK_CACHE_TTL) and the engine already reads it that often.
    1_000,
  );

  // The Trading tab's list and key, not `/api/chart/trades`. That one is this
  // machine's database alone, so a position the VPS opened ("Node: Remote")
  // was missing from the chart while the Trading tab and the Dashboard showed
  // it (2026-09-28). Same key as theirs, so no extra bridge reads.
  const trades = usePoll<Trade[]>(
    "trading/trades",
    useCallback(() => api.get<Trade[]>("/api/trading/trades"), []),
    5_000,
  );

  const refreshAll = useCallback(async () => {
    await Promise.all([candles.refresh(), overlays.refresh(), tick.refresh(), trades.refresh()]);
  }, [candles, overlays, tick, trades]);

  return useMemo(
    () => ({
      timeframe, setTimeframe, count, setCount,
      candles, overlays, tick, trades, refreshAll,
    }),
    [timeframe, count, candles, overlays, tick, trades, refreshAll],
  );
}
