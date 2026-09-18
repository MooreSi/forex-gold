import { useCallback, useMemo, useState } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import type { BlackoutSettings, NewsState } from "@/api/types";

/** Currencies whose releases move gold. The page's own filter, kept as data. */
export const GOLD_CURRENCIES = ["USD", "EUR", "GBP", "CNY", "XAU"];

export function useNewsController() {
  const [goldOnly, setGoldOnly] = useState(true);
  const [upcomingOnly, setUpcomingOnly] = useState(true);

  const state = usePoll<NewsState>(
    "news/state",
    useCallback(() => api.get<NewsState>("/api/news/state"), []),
    30_000,
  );

  const refresh = useCallback(async () => {
    // A hard refresh drops the server's cache; the ordinary poll must not, or
    // every tick re-fetches the upstream feed.
    await api.post<NewsState>("/api/news/refresh");
    await state.refresh();
  }, [state]);

  const saveBlackout = useCallback(
    async (next: { enabled: boolean; minutes_before: number; minutes_after: number }) => {
      await api.put<BlackoutSettings>("/api/news/blackout", next);
      await state.refresh();
    },
    [state],
  );

  const events = useMemo(() => {
    const all = state.data?.events ?? [];
    const now = Date.now() / 1000;
    return all.filter(
      (e) =>
        (!goldOnly || GOLD_CURRENCIES.includes(e.currency)) &&
        (!upcomingOnly || e.ts >= now),
    );
  }, [state.data, goldOnly, upcomingOnly]);

  return {
    state, events, goldOnly, setGoldOnly, upcomingOnly, setUpcomingOnly,
    refresh, saveBlackout,
  };
}
