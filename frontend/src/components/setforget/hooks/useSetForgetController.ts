import { useCallback, useMemo, useState } from "react";
import { api, ApiError } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import type { Candle, Overlays, SetForgetState } from "@/api/types";

/** The window the section's chart draws. 4H is the entry timeframe. */
export const CHART_TIMEFRAME = "4H";
// At least as many bars as the slowest average, or EMA 200 comes back all
// nulls and the chart quietly draws one line under a two-line legend.
export const CHART_COUNT = 300;

/**
 * The two averages Alex G's confluence checklist reads, not the Chart tab's
 * 9/21/50. They are asked for from the backend rather than computed here: the
 * same `ema_series` answers for the engine's signal snapshot, and a second
 * implementation in TypeScript would be a second answer to what an EMA 200 is.
 */
export const CHART_EMAS = "50,200";

/** 0 means "size it from Risk per trade % and the stop distance". */
export const SIZE_FROM_RISK = 0;

/**
 * The lot sizes the selector offers, plus whatever the account is already set
 * to. Gold's minimum is 0.01 and the steps are the ones a person actually
 * types; a free-text box for a number that goes to a broker invites a typo
 * with two extra zeros on it.
 */
export const LOT_STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1] as const;

interface Outcome {
  ok: boolean;
  text: string;
}

/**
 * All of the Set & Forget section's state and fetching.
 *
 * Two reads, deliberately separate, mirroring the backend's own split:
 *
 * - the poll is the FREE one. Evidence, zones, the rules' candidate and the
 *   checklist, refreshed on a slow interval because a 4H setup does not move
 *   between blinks. Nothing is billed, so looking at the page costs nothing.
 * - `evaluate()` is the BILLABLE one, and only runs when the operator presses
 *   the button. Its answer replaces the poll's until the next refresh.
 *
 * Execution goes to the EXISTING money endpoints — `/api/trading/orders/market`
 * and `/api/trading/orders/limit`, the same two the manual dialogs use. There
 * is no Set & Forget order path, on purpose: a second one would drift from the
 * first, and this app has exactly one.
 */
export function useSetForgetController() {
  const state = usePoll<SetForgetState>(
    "trading/setforget",
    useCallback(() => api.get<SetForgetState>("/api/trading/setforget"), []),
    60_000,
  );
  const candles = usePoll<Candle[]>(
    `setforget/candles/${CHART_TIMEFRAME}`,
    useCallback(
      () => api.get<Candle[]>(
        `/api/chart/candles?timeframe=${CHART_TIMEFRAME}&count=${CHART_COUNT}`),
      [],
    ),
    60_000,
  );
  const overlays = usePoll<Overlays>(
    `setforget/overlays/${CHART_TIMEFRAME}/${CHART_EMAS}`,
    useCallback(
      () => api.get<Overlays>(
        `/api/chart/overlays?timeframe=${CHART_TIMEFRAME}&count=${CHART_COUNT}`
        + `&emas=${CHART_EMAS}`),
      [],
    ),
    60_000,
  );

  // The billable answer, held separately so a poll tick cannot quietly
  // overwrite the review the operator is reading and about to act on.
  const [review, setReview] = useState<SetForgetState | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  const [placing, setPlacing] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [lots, setLots] = useState<number | null>(null);

  const data = review ?? state.data;
  const candidate = data?.candidate ?? null;

  /**
   * The lot size in force: what the operator picked, else the stored setting,
   * else the risk-based suggestion. Null means "none chosen yet", which the
   * box renders as no amount rather than as zero.
   */
  const effectiveLots = useMemo(() => {
    if (lots !== null) return lots;
    if (data?.lot_size) return data.lot_size;
    return data?.suggested_lot ?? null;
  }, [lots, data?.lot_size, data?.suggested_lot]);

  const evaluate = useCallback(async () => {
    setEvaluating(true);
    setOutcome(null);
    try {
      setReview(await api.post<SetForgetState>("/api/trading/setforget/evaluate"));
    } catch (e) {
      setOutcome({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setEvaluating(false);
    }
  }, []);

  const saveLotSize = useCallback(async (value: number) => {
    setLots(value);
    try {
      await api.put("/api/trading/setforget/settings", { lot_size: value });
    } catch (e) {
      setOutcome({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
    }
  }, []);

  /**
   * Place the setup that is on screen.
   *
   * The levels come from the rendered candidate, not from a fresh read. The
   * chart moves as price does, and re-reading on the way to the broker would
   * place a trade against numbers nobody saw. Same rule the ORB card follows.
   */
  const execute = useCallback(async () => {
    if (!candidate || !data) return;
    setPlacing(true);
    setOutcome(null);
    try {
      const lotSize = effectiveLots && effectiveLots > 0 ? effectiveLots : null;
      const res = candidate.order_type === "limit"
        ? await api.post<{ mt5_ticket?: number; price?: number }>(
            "/api/trading/orders/limit",
            {
              direction: candidate.direction,
              entry_low: candidate.entry,
              entry_high: candidate.entry,
              stop_loss: candidate.stop_loss,
              tp1: candidate.take_profit,
              lot_size: lotSize,
              notes: `${data.source_name} — ${data.ai?.verdict ?? "rules"} setup`,
            })
        : await api.post<{ mt5_ticket?: number; entry_price?: number; where?: string }>(
            "/api/trading/orders/market",
            {
              direction: candidate.direction,
              stop_loss: candidate.stop_loss,
              take_profit: candidate.take_profit,
              lot_size: lotSize,
              strategy: data.strategy,
              source_name: data.source_name,
            });
      const ticket = (res as { mt5_ticket?: number }).mt5_ticket;
      setOutcome({
        ok: true,
        text: candidate.order_type === "limit"
          ? `Limit order resting at ${candidate.entry.toFixed(2)} · ticket ${ticket ?? "—"}`
          : `Opened at market · ticket ${ticket ?? "—"}`,
      });
      await state.refresh();
    } catch (e) {
      setOutcome({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setPlacing(false);
    }
  }, [candidate, data, effectiveLots, state]);

  return useMemo(
    () => ({
      data, candidate, loading: state.loading && !data, error: state.error,
      candles: candles.data ?? [], overlays: overlays.data ?? null,
      evaluate, evaluating,
      execute, placing, outcome, setOutcome,
      lots: effectiveLots, setLots, saveLotSize,
      refresh: state.refresh,
    }),
    [data, candidate, state.loading, state.error, state.refresh,
     candles.data, overlays.data, evaluate, evaluating, execute, placing,
     outcome, effectiveLots, saveLotSize],
  );
}

export type SetForgetController = ReturnType<typeof useSetForgetController>;
