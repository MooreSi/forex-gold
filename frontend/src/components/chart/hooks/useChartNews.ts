import { useCallback, useMemo, useState } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import {
  measuredCorrectionS, NEWS_IMPACTS, type ChartNewsEvent, type NewsFilter,
} from "../internal/newsMarks";

const KEY = "chart-news";
const DAY = 86_400;
// The server refuses more than 400 days; the oldest bars past that go unmarked.
const MAX_SPAN_DAYS = 399;

interface Saved {
  on: boolean;
  impacts: string[];
  allCurrencies: boolean;
}

const DEFAULTS: Saved = { on: false, impacts: [...NEWS_IMPACTS], allCurrencies: false };

function load(): Saved {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "null") as Partial<Saved> | null;
    return raw ? { ...DEFAULTS, ...raw } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

/** What CandleChart needs to draw the news, or null with the button off. */
export interface ChartNewsLayer {
  events: ChartNewsEvent[];
  correctionS: number;
  filter: NewsFilter;
}

/**
 * The NEWS button's state and its events, for the Dashboard's chart and the
 * Chart tab's Broker chart (asked for 2026-10-21: "highlight on the chart
 * where a news event occurred so you can see the impact of the event").
 *
 * The choice is remembered per browser and shared by both charts. With the
 * button off nothing is fetched. The window runs from a day before the
 * oldest bar to two days ahead, in whole days, so the poll key moves once a
 * day rather than with every candle.
 */
export function useChartNews(oldestBarTs: number | undefined, tickTs: number | undefined) {
  const [saved, setSaved] = useState<Saved>(load);
  const update = useCallback((patch: Partial<Saved>) => {
    setSaved((prev) => {
      const next = { ...prev, ...patch };
      try {
        localStorage.setItem(KEY, JSON.stringify(next));
      } catch {
        // Still applies; just not remembered.
      }
      return next;
    });
  }, []);

  const today = Math.floor(Date.now() / 1000 / DAY) * DAY;
  const to = today + 2 * DAY;
  const oldest = oldestBarTs === undefined ? today : Math.floor(oldestBarTs / DAY) * DAY - DAY;
  const from = Math.max(oldest, to - MAX_SPAN_DAYS * DAY);
  const wanted = saved.on && oldestBarTs !== undefined;

  const poll = usePoll<{ events: ChartNewsEvent[] }>(
    wanted ? `calendar/chart_news?${from}&${to}` : "calendar/chart_news:off",
    useCallback(
      () => (wanted
        ? api.get<{ events: ChartNewsEvent[] }>(`/api/calendar/chart_news?from_ts=${from}&to_ts=${to}`)
        : Promise.resolve({ events: [] })),
      [wanted, from, to],
    ),
    300_000,
  );

  const correctionS = measuredCorrectionS(tickTs, Date.now() / 1000);
  const events = poll.data?.events;
  const layer = useMemo<ChartNewsLayer | null>(
    () => (saved.on
      ? {
        events: Array.isArray(events) ? events : [],
        correctionS,
        filter: { impacts: new Set(saved.impacts), allCurrencies: saved.allCurrencies },
      }
      : null),
    [saved, events, correctionS],
  );

  return {
    on: saved.on,
    impacts: saved.impacts,
    allCurrencies: saved.allCurrencies,
    toggle: () => update({ on: !saved.on }),
    toggleImpact: (impact: string) => update({
      impacts: saved.impacts.includes(impact)
        ? saved.impacts.filter((i) => i !== impact)
        : [...saved.impacts, impact],
    }),
    setAllCurrencies: (all: boolean) => update({ allCurrencies: all }),
    error: poll.error,
    layer,
  };
}

export type ChartNews = ReturnType<typeof useChartNews>;
