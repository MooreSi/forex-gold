import { useCallback, useState } from "react";
import { api, ApiError } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";

export interface StrategySpec {
  key: string;
  label: string;
  kind: string;
  summary: string;
}

export interface ChannelStrategy {
  source: string;
  strategy_override: string | null;
  auto_strategy: boolean;
  lot_mult: number;
  win_rate?: number;
  sample_n?: number;
  net_pnl?: number;
}

export interface Recommendation {
  strategy?: string;
  label?: string;
  summary?: string;
  reason?: string;
}

/**
 * Which strategy each signal source trades under.
 *
 * The screen was never ported. Everything it needs has been on the backend
 * since the port — and `GET /api/trading/channel-strategies` answered 500 the
 * whole time because the handler was annotated `-> dict` and returned a list.
 * Nothing called it, so nothing noticed.
 *
 * Two polls, not one: the catalogue is content that ships with the build, and
 * the per-channel assignments are state. A recommendation is fetched only when
 * asked for, because the paid one costs money and the free one is a read of
 * whatever the paid one last wrote.
 */
export function useStrategyController() {
  const [busy, setBusy] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [recs, setRecs] = useState<Record<string, Recommendation>>({});

  const catalogue = usePoll<{ catalogue: StrategySpec[]; custom: unknown }>(
    "trading/strategies",
    useCallback(() => api.get<{ catalogue: StrategySpec[]; custom: unknown }>(
      "/api/trading/strategies"), []),
    300_000,
  );

  const channels = usePoll<{ channels: ChannelStrategy[] }>(
    "trading/channel-strategies",
    useCallback(() => api.get<{ channels: ChannelStrategy[] }>(
      "/api/trading/channel-strategies"), []),
    30_000,
  );

  const assign = useCallback(
    async (source: string, strategy: string | null, auto: boolean) => {
      setBusy(source);
      setRefusal(null);
      try {
        await api.post("/api/trading/channel-strategies", { source, strategy, auto });
        await channels.refresh();
      } catch (e) {
        setRefusal(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [channels],
  );

  /** The recommendations already computed. Free: a read of the local record. */
  const loadRecommendations = useCallback(async (sources: string[]) => {
    if (sources.length === 0) return;
    setRefusal(null);
    try {
      const body = await api.get<{ recommendations: Record<string, Recommendation> }>(
        `/api/trading/channel-strategies/recommendations?sources=${
          encodeURIComponent(sources.join(","))}`);
      setRecs(body.recommendations ?? {});
    } catch (e) {
      setRefusal(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  /** Ask the AI for a fresh recommendation. **Billable.** */
  const requestRecommendations = useCallback(async (sources: string[]) => {
    setBusy("recommend");
    setRefusal(null);
    try {
      const body = await api.post<{ recommendations: Record<string, Recommendation> }>(
        "/api/trading/channel-strategies/recommend", { sources });
      setRecs((prev) => ({ ...prev, ...(body.recommendations ?? {}) }));
    } catch (e) {
      setRefusal(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  return {
    catalogue: asArray<StrategySpec>(catalogue.data?.catalogue),
    channels: asArray<ChannelStrategy>(channels.data?.channels),
    loading: !channels.data && !channels.error,
    error: channels.error,
    recs, busy, refusal,
    assign, loadRecommendations, requestRecommendations,
  };
}
