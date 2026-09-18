import { useCallback, useState } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";
import type { HistoryState, HourlyCell } from "@/api/types";

export const WINDOWS = [7, 30, 90, 365];

export function useHistoryController() {
  const [days, setDays] = useState(30);
  const [recomputing, setRecomputing] = useState(false);

  // Keyed by window, because 7 days and 365 days are different questions and
  // one key would serve whichever panel asked last.
  const state = usePoll<HistoryState>(
    `history/state?days=${days}`,
    useCallback(() => api.get<HistoryState>(`/api/history/state?days=${days}`), [days]),
    15_000,
  );

  const recompute = useCallback(async () => {
    setRecomputing(true);
    try {
      await api.post(`/api/history/recompute?days=${days}`);
      await state.refresh();
    } finally {
      setRecomputing(false);
    }
  }, [days, state]);

  const setChannelPaused = useCallback(
    async (source: string, paused: boolean) => {
      await api.put("/api/history/channel-paused", { source, paused });
      await state.refresh();
    },
    [state],
  );

  return {
    days, setDays, state, recompute, recomputing, setChannelPaused,
    hourly: asArray<HourlyCell>(state.data?.hourly),
    channels: asArray<Record<string, unknown>>(state.data?.channels),
  };
}
