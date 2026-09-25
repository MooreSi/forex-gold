import { useCallback, useMemo } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { useHeaderState } from "@/hooks/useHeaderState";
import { useChartController } from "@/components/chart/hooks/useChartController";
import { useClosedTrades } from "@/components/history/hooks/useClosedTrades";
import { asArray, asObject } from "@/lib/asArray";
import type { Candle, HistoryState, NewsState, SetForgetState, Trade } from "@/api/types";

/** The window every figure on this screen is measured over. */
export const DASHBOARD_DAYS = 30;

/**
 * How often the open positions are re-read, in ms.
 *
 * The Trading tab asks for these every 5s. The Dashboard is the screen left
 * open while a position runs, so it asks for 3s — the cadence the chart's own
 * tick poll already uses, which is what the running P&L moves with. Since
 * 2026-09-23 `usePoll` gives a shared key the SHORTEST interval any live
 * subscriber asked for, so this is honoured whichever tab was opened first;
 * before that it silently depended on the order.
 *
 * **Not faster than this.** `/api/trading/trades` reaches the MT5 bridge with
 * no cache in front of it, and uncached per-panel polling of the bridge is
 * what stalled the event loop in bugs/030 — 388 round trips in 25 seconds.
 * The number this screen shows is the broker's own, so polling faster than
 * the broker's price arrives buys nothing and costs the bridge.
 */
export const POSITIONS_INTERVAL_MS = 3_000;

/**
 * How many daily candles to ask for when one is wanted.
 *
 * `/api/chart/candles` validates `count` as `ge=10`. Asking for fewer is a
 * 422, not a short answer.
 */
export const DAILY_COUNT = 10;

interface EnginesState {
  engines: { id: string; label: string; running: boolean; built: boolean }[];
  settings: Record<string, unknown>;
}

interface ScheduleState {
  markets?: Record<string, unknown> | null;
}

/**
 * Everything the Dashboard reads, through the keys the other tabs already use.
 *
 * **Not one new poll key in here.** `usePoll` dedups by key, so every fetch
 * below is the same fetch the tab that owns it would make — the header's 5s
 * read, the chart's candles, the Analysis window, the free Set & Forget read.
 * A private `dashboard/...` key for any of them would double that endpoint's
 * traffic for as long as this tab is open, which on the 5s header poll is a
 * request every five seconds for a number the shell is already holding.
 *
 * That is also why the tick is taken from `/api/system/header` rather than
 * `/api/chart/tick`: the header payload already carries it.
 *
 * Nothing here is billable. The AI figures are read back from what the AI
 * Analysis tab last produced (`useMarketResearch`, a free GET); this screen
 * has no button that asks a model, on purpose — it is the tab left open all
 * day.
 */
export function useDashboardController() {
  const header = useHeaderState();
  const chart = useChartController();
  const closed = useClosedTrades(DASHBOARD_DAYS);

  const history = usePoll<HistoryState>(
    `history/state?days=${DASHBOARD_DAYS}`,
    useCallback(
      () => api.get<HistoryState>(`/api/history/state?days=${DASHBOARD_DAYS}`), []),
    15_000,
  );

  const setforget = usePoll<SetForgetState>(
    "trading/setforget",
    useCallback(() => api.get<SetForgetState>("/api/trading/setforget"), []),
    60_000,
  );

  // The open positions, from the TRADING tab's key rather than the chart's.
  // `/api/chart/trades` exists to draw markers and answers `pnl: null` for
  // every row; `/api/trading/trades` carries the broker's running profit and
  // the `untracked` flag. The chart card still reads the chart's copy, which
  // is what it is for.
  const positions = usePoll<Trade[]>(
    "trading/trades",
    useCallback(() => api.get<Trade[]>("/api/trading/trades"), []),
    POSITIONS_INTERVAL_MS,
  );

  const signals = usePoll<Record<string, unknown>[]>(
    "trading/signals",
    useCallback(() => api.get<Record<string, unknown>[]>("/api/trading/signals"), []),
    10_000,
  );

  const news = usePoll<NewsState>(
    "news/state",
    useCallback(() => api.get<NewsState>("/api/news/state"), []),
    30_000,
  );

  const engines = usePoll<EnginesState>(
    "engines/state",
    useCallback(() => api.get<EnginesState>("/api/engines/state"), []),
    5_000,
  );

  const schedule = usePoll<ScheduleState>(
    "schedule/state",
    useCallback(() => api.get<ScheduleState>("/api/schedule/state"), []),
    10_000,
  );

  // Daily bars, on the same key shape the Chart tab uses. The LAST one is the
  // day that is forming, and its OPEN is what "today's move" is measured
  // against -- see `dayChange` in dashboardMath.ts for why that reference and
  // not another.
  //
  // Ten, although one is read. `/api/chart/candles` declares `count` as
  // `ge=10`, so asking for the two that are wanted is a 422 and the price
  // header shows an em dash for a figure the feed could have answered. Found
  // in the running app on 2026-09-22; the test suite's stubbed fetch had
  // answered the request happily.
  const daily = usePoll<Candle[]>(
    `chart/candles?1D&count=${DAILY_COUNT}`,
    useCallback(
      () => api.get<Candle[]>(`/api/chart/candles?timeframe=1D&count=${DAILY_COUNT}`), []),
    60_000,
  );

  // The stored limits, on the slowest interval here. They are settings rather
  // than live data, but they are settings another node or another tab can
  // change under this screen, and a risk figure that is an hour stale is one
  // the operator would act on. `useSettingsResource` is the read the Risk
  // section uses; it also owns a save path this screen deliberately has none
  // of, so this is the plain GET.
  const risk = usePoll<Record<string, unknown>>(
    "settings/risk",
    useCallback(() => api.get<Record<string, unknown>>("/api/settings/risk"), []),
    60_000,
  );

  return useMemo(() => ({
    header,
    chart,
    closed,
    history,
    setforget,
    news,
    daily,
    risk,
    signals: asArray<Record<string, unknown>>(signals.data),
    engines: asArray<EnginesState["engines"][number]>(engines.data?.engines),
    markets: asObject(schedule.data?.markets),
    /** For the chart's markers. No running profit on these rows. */
    chartTrades: asArray<Trade>(chart.trades.data),
    /** For the positions card. These carry the broker's P&L. */
    positions: asArray<Trade>(positions.data),
    performance: asObject(history.data?.performance),
  }), [header, chart, closed, history, setforget, news, daily, risk,
       signals.data, engines.data, schedule.data, positions.data]);
}
