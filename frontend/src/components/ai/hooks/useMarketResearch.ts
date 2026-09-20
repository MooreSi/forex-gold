import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";

/**
 * The market analysis, exactly as `claude_ai.request_market_analysis` returns
 * it. Every field is optional: a provider that answers half the schema must
 * reduce what the screen shows, never blank it.
 */
export interface MarketAnalysis {
  sentiment?: string;
  sentiment_confidence?: number;
  today_bias?: string;
  price_low?: number;
  price_high?: number;
  summary?: string;
  technical_summary?: string;
  key_drivers?: string[];
  risk_factors?: string[];
  support_levels?: number[];
  resistance_levels?: number[];
  strategy_recommendation?: string;
  strategy_label?: string;
  strategy_reason?: string;
  strategy_summary?: string;
  signal_analysis?: string;
  disclaimer?: string;
  generated_at?: string;
  news_count?: number;
  fetched_news?: boolean;
  error?: string;
}

interface ResearchBody {
  analysis: MarketAnalysis | null;
  saved_at: string;
}

/**
 * One question, one call, one answer.
 *
 * The tab loads what was last found — free, and no model involved — so that
 * opening it costs nothing. Research asks the model **once** about every
 * metric at the same time, which is what the NiceGUI tab did: one coherent
 * view. A call per metric costs several and produces several views that can
 * contradict each other.
 */
export function useMarketResearch() {
  const [analysis, setAnalysis] = useState<MarketAnalysis | null>(null);
  const [savedAt, setSavedAt] = useState("");
  const [researching, setResearching] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void api.get<ResearchBody>("/api/ai/research")
      .then((b) => {
        if (cancelled) return;
        setAnalysis(b.analysis ?? null);
        setSavedAt(b.saved_at ?? "");
      })
      .catch(() => { /* Nothing stored is the normal first-run state. */ })
      .finally(() => !cancelled && setLoaded(true));
    return () => { cancelled = true; };
  }, []);

  const research = useCallback(async () => {
    setResearching(true);
    setRefusal(null);
    try {
      const b = await api.post<ResearchBody>("/api/ai/research", {});
      setAnalysis(b.analysis ?? null);
      setSavedAt(b.saved_at ?? "");
    } catch (e) {
      // The backend's words, unchanged. "No AI provider is configured" tells
      // the operator what to do; "request failed" does not.
      setRefusal(e instanceof ApiError ? e.message : String(e));
    } finally {
      setResearching(false);
    }
  }, []);

  return { analysis, savedAt, researching, refusal, research, loaded };
}
