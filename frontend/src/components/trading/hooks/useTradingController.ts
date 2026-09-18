import { useCallback, useMemo } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { asArray, asObject } from "@/lib/asArray";
import type { HaltState, Trade } from "@/api/types";

interface ScheduleState {
  schedule: Record<string, unknown>;
  enabled: boolean;
  daily_target: number;
  daily_state: Record<string, unknown>;
  clock: Record<string, unknown>;
}

interface TemplatesState {
  templates: Record<string, unknown>[];
  builtin: string;
  ea_connected: boolean;
  ea_last_seen_secs: number | null;
}

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

  const schedule = usePoll<ScheduleState>(
    "schedule/state",
    useCallback(() => api.get<ScheduleState>("/api/schedule/state"), []),
    10_000,
  );

  const templates = usePoll<TemplatesState>(
    "trading/templates",
    useCallback(() => api.get<TemplatesState>("/api/trading/templates"), []),
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

  // ── Schedule ──────────────────────────────────────────────────────────────

  const setScheduleEnabled = useCallback(async (enabled: boolean) => {
    await api.put("/api/schedule/enabled", { enabled });
    await schedule.refresh();
  }, [schedule]);

  const setSchedule = useCallback(async (next: Record<string, unknown>) => {
    await api.put("/api/schedule/schedule", { schedule: next });
    await schedule.refresh();
  }, [schedule]);

  const setDailyTarget = useCallback(async (target: number) => {
    await api.put("/api/schedule/daily-target", { target });
    await schedule.refresh();
  }, [schedule]);

  const resumeToday = useCallback(async () => {
    await api.post("/api/schedule/resume-today");
    await schedule.refresh();
  }, [schedule]);

  // ── EA templates ──────────────────────────────────────────────────────────

  const saveTemplate = useCallback(
    async (name: string, values: Record<string, unknown>) => {
      const body = await api.put<{ pushed: boolean }>(
        `/api/trading/templates/${encodeURIComponent(name)}`, values);
      await templates.refresh();
      return { pushed: body.pushed === true };
    },
    [templates],
  );

  const deleteTemplate = useCallback(async (name: string) => {
    await api.del(`/api/trading/templates/${encodeURIComponent(name)}`);
    await templates.refresh();
  }, [templates]);

  const installBuiltin = useCallback(async () => {
    await api.post("/api/trading/templates/install-builtin");
    await templates.refresh();
  }, [templates]);

  return useMemo(
    () => ({
      trades, halt, signals, disabledReason, refreshAll,
      schedule: schedule.data,
      setScheduleEnabled, setSchedule, setDailyTarget, resumeToday,
      templates: asArray<Record<string, unknown>>(templates.data?.templates),
      eaConnected: asObject(templates.data)["ea_connected"] === true,
      eaLastSeen: typeof templates.data?.ea_last_seen_secs === "number"
        ? templates.data.ea_last_seen_secs
        : null,
      saveTemplate, deleteTemplate, installBuiltin,
    }),
    [trades, halt, signals, disabledReason, refreshAll, schedule.data,
     setScheduleEnabled, setSchedule, setDailyTarget, resumeToday,
     templates.data, saveTemplate, deleteTemplate, installBuiltin],
  );
}
