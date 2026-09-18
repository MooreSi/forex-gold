import { useCallback, useMemo, useState } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import type { Candle, Overlays, Tick, Trade } from "@/api/types";

export const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1H", "4H", "1D"] as const;
export type Timeframe = (typeof TIMEFRAMES)[number];

/**
 * All of the Chart tab's state and fetching, in one place, so `ChartPanel`
 * stays composition.
 *
 * Three polls, deliberately, at the cadences the NiceGUI page used and for the
 * same reasons: the tick moves every second, candles and their overlays move
 * once a bar, and open trades change only when something happens. One interval
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
    3_000,
  );

  const trades = usePoll<Trade[]>(
    "chart/trades",
    useCallback(() => api.get<Trade[]>("/api/chart/trades"), []),
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
