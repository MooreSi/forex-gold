import { useCallback } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";
import type { ParsingState } from "@/api/types";

export function useParsingController() {
  const state = usePoll<ParsingState>(
    "parsing/state",
    useCallback(() => api.get<ParsingState>("/api/parsing/state"), []),
    10_000,
  );

  const messages = usePoll<{ messages: Record<string, unknown>[]; total: number }>(
    "parsing/messages",
    useCallback(
      () => api.get<{ messages: Record<string, unknown>[]; total: number }>(
        "/api/parsing/messages?limit=100"),
      [],
    ),
    15_000,
  );

  const pending = usePoll<{ pending: Record<string, unknown>[] }>(
    "parsing/unrecognised",
    useCallback(
      () => api.get<{ pending: Record<string, unknown>[] }>("/api/parsing/unrecognised"),
      [],
    ),
    15_000,
  );

  const saveSetting = useCallback(
    async (key: string, value: number) => {
      await api.put("/api/parsing/settings", { [key]: value });
      await state.refresh();
    },
    [state],
  );

  const saveLexicon = useCallback(
    async (category: string, phrases: string[]) => {
      await api.put("/api/parsing/lexicon", { category, phrases });
      await state.refresh();
    },
    [state],
  );

  const setChannelEnabled = useCallback(
    async (channel: string, enabled: boolean) => {
      await api.put("/api/parsing/channel-parser", { channel, enabled });
      await state.refresh();
    },
    [state],
  );

  const resolve = useCallback(
    async (rowId: number, status: string, channel?: string, rule?: Record<string, unknown>) => {
      await api.post("/api/parsing/unrecognised/resolve", {
        row_id: rowId, status, channel, rule,
      });
      await pending.refresh();
    },
    [pending],
  );

  return {
    state,
    messages: asArray<Record<string, unknown>>(messages.data?.messages),
    messageTotal: messages.data?.total ?? 0,
    pending: asArray<Record<string, unknown>>(pending.data?.pending),
    saveSetting, saveLexicon, setChannelEnabled, resolve,
  };
}
