import { useCallback, useMemo } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import type { HaltState, Trade } from "@/api/types";

/**
 * The Trading tab's reads, and the one question every control on it asks:
 * **may this act right now, and if not, why not?**
 *
 * `disabledReason` is computed from what the backend said — a halt reason, a
 * closed market, a tripped breaker, a bridge that is not connected. It is
 * never a decision made here. The page renders the answer; the backend decides
 * it, and duplicating a risk check in the UI produces two answers that drift.
 */
export function useTradingController() {
  const trades = usePoll<Trade[]>(
    "trading/trades",
    useCallback(() => api.get<Trade[]>("/api/trading/trades"), []),
    5_000,
  );

  const halt = usePoll<HaltState>(
    "trading/halt",
    useCallback(() => api.get<HaltState>("/api/trading/halt"), []),
    5_000,
  );

  const signals = usePoll<Record<string, unknown>[]>(
    "trading/signals",
    useCallback(() => api.get<Record<string, unknown>[]>("/api/trading/signals"), []),
    10_000,
  );

  const disabledReason = useMemo<string | null>(() => {
    const h = halt.data;
    if (!h) return halt.error ? "Trading status is unknown — the app cannot reach the backend." : null;
    if (h.reason) return h.reason;
    if (h.market_closed) return "The market is closed for the week.";
    const breaker = h.circuit_breaker;
    if (breaker && breaker["tripped"] === true) {
      return typeof breaker["reason"] === "string"
        ? `Circuit breaker: ${breaker["reason"]}`
        : "The circuit breaker has tripped.";
    }
    return null;
  }, [halt.data, halt.error]);

  const refreshAll = useCallback(async () => {
    await Promise.all([trades.refresh(), halt.refresh(), signals.refresh()]);
  }, [trades, halt, signals]);

  return useMemo(
    () => ({ trades, halt, signals, disabledReason, refreshAll }),
    [trades, halt, signals, disabledReason, refreshAll],
  );
}
