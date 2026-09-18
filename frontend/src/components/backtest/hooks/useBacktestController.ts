import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import { asArray } from "@/lib/asArray";
import type { BacktestOptions, BacktestResult } from "@/api/types";

/**
 * The numeric fields are held as STRINGS.
 *
 * Not laziness: an input whose state is a number cannot hold a half-typed
 * decimal. `Number("1.")` is 1, so re-rendering from the number drops the
 * point the operator just typed and "1.25" arrives as 125 — a spread of 125
 * points instead of 1.25, which turns a profitable backtest into a disaster
 * and looks like a strategy result. Found by the test of the same name.
 *
 * They are converted once, on submit.
 */
export const NUMERIC_FIELDS = [
  "days", "starting_balance", "risk_pct", "lots_per_trade", "spread_pts",
  "commission_per_lot", "max_sl_pts", "split_fraction",
] as const;

export type NumericField = (typeof NUMERIC_FIELDS)[number];

export type BacktestForm = {
  timeframe: string;
  granularity: string;
  live_trades_only: boolean;
} & Record<NumericField, string>;

/** The engine's own defaults, repeated here so the form opens on them rather
 *  than on blanks the backend would have to guess at. */
export const DEFAULTS: BacktestForm = {
  timeframe: "M5",
  granularity: "candles",
  live_trades_only: false,
  days: "30",
  starting_balance: "1000",
  risk_pct: "1",
  lots_per_trade: "0",
  spread_pts: "0.4",
  commission_per_lot: "7",
  max_sl_pts: "50",
  split_fraction: "0",
};

export function useBacktestController() {
  const [options, setOptions] = useState<BacktestOptions | null>(null);
  const [form, setForm] = useState<BacktestForm>(DEFAULTS);
  const [selected, setSelected] = useState<string[]>([]);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [running, setRunning] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api
      .get<BacktestOptions>("/api/backtest/options")
      // Normalised on arrival, not trusted. Every list here is mapped over
      // directly in the form, and a half-deployed backend that answers `{}`
      // would throw inside render and take the whole dashboard down with it —
      // the same failure `asArray` exists for.
      .then((o) => !cancelled && setOptions({
        strategies: asArray(o?.strategies),
        templates: asArray(o?.templates),
        timeframes: asArray<string>(o?.timeframes),
        granularities: asArray<string>(o?.granularities),
        min_trades_per_side: Number(o?.min_trades_per_side ?? 0),
        broker_tz_offset: Number(o?.broker_tz_offset ?? 0),
      }))
      .catch((e: Error) => !cancelled && setRefusal(e.message));
    return () => {
      cancelled = true;
    };
  }, []);

  const set = useCallback(<K extends keyof BacktestForm>(key: K, value: BacktestForm[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
  }, []);

  const toggle = useCallback((key: string) => {
    setSelected((s) => (s.includes(key) ? s.filter((k) => k !== key) : [...s, key]));
  }, []);

  const run = useCallback(async () => {
    setRunning(true);
    setRefusal(null);
    try {
      const numbers = Object.fromEntries(
        NUMERIC_FIELDS.map((k) => [k, Number(form[k]) || 0]),
      );
      const body = await api.post<BacktestResult>("/api/backtest/run", {
        timeframe: form.timeframe,
        granularity: form.granularity,
        live_trades_only: form.live_trades_only,
        ...numbers,
        strategies: selected,
      });
      setResult({ ...body, results: asArray(body.results) });
    } catch (e) {
      // A refusal here is actionable ("the bridge is not connected"); anything
      // else is ours and says so.
      setRefusal(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }, [form, selected]);

  return { options, form, set, selected, toggle, result, running, refusal, run };
}
