import { useCallback, useState } from "react";
import { api, ApiError } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";

export interface EngineRow {
  id: string;
  label: string;
  running: boolean;
  built: boolean;
}

export interface EnginesState {
  engines: EngineRow[];
  /**
   * The settings the engines are OBEYING, which in Remote mode are the peer's
   * rather than this machine's row. The backend overlays them; this side just
   * renders what it is given.
   */
  settings: Record<string, unknown>;
  pro_model: Record<string, unknown>;
  /**
   * Which machine a control here will reach: "local", "remote" or
   * "centralized". Absent on an older backend, which the panel reads as
   * "local" — the state this tab has always assumed.
   */
  control_target?: string;
  /** The engines whose AI-evaluation switch the sync protocol can carry. */
  ai_eval_keys?: Record<string, string>;
}

export interface ReversalReport {
  realised: Record<string, unknown>;
  edge: Record<string, unknown>;
  shadow: unknown;
  history: unknown;
  /** The learning curve, and how far the gate is from training. Guarded on
   *  the backend, so an install with no ML table sends empty objects here
   *  rather than failing the whole report. */
  ml?: { metrics: Record<string, unknown>; summary: Record<string, unknown> };
  /** Win rate against the no-edge rate SL/(SL+TP). Guarded: {} when it
   *  cannot be computed. docs/todo/reversal-engine/220. */
  benchmark?: unknown;
  /** Other markets against gold, and the meta-labeller with and without them
   *  at every refit. Guarded: {} when unreadable. reversal-engine/230. */
  cross_asset?: unknown;
}

export function useEnginesController() {
  const [busy, setBusy] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [study, setStudy] = useState<string | null>(null);

  const state = usePoll<EnginesState>(
    "engines/state",
    useCallback(() => api.get<EnginesState>("/api/engines/state"), []),
    5_000,
  );

  // The engine's own measurements: real closed P&L, the variant scoreboard and
  // the virtual trade history. Its own poll and a slow one -- none of it moves
  // between signals, and it is a row per variant per signal.
  const report = usePoll<ReversalReport>(
    "engines/reversal/report",
    useCallback(() => api.get<ReversalReport>("/api/engines/reversal/report"), []),
    30_000,
  );

  const setRunning = useCallback(
    async (engine: string, running: boolean) => {
      setBusy(engine);
      setRefusal(null);
      try {
        await api.post("/api/engines/running", { engine, running });
        await state.refresh();
      } catch (e) {
        setRefusal(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [state],
  );

  const saveSetting = useCallback(
    async (key: string, value: number) => {
      await api.put("/api/engines/settings", { [key]: value });
      await state.refresh();
    },
    [state],
  );

  const refit = useCallback(async () => {
    setBusy("fit");
    try {
      await api.post("/api/engines/reversal/fit");
      await state.refresh();
    } finally {
      setBusy(null);
    }
  }, [state]);

  const runStudy = useCallback(async () => {
    setBusy("study");
    setRefusal(null);
    try {
      const body = await api.post<{ report: string }>("/api/engines/reversal/study");
      setStudy(body.report);
    } catch (e) {
      setRefusal(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  return {
    state,
    engines: asArray<EngineRow>(state.data?.engines),
    busy, refusal, study, setRunning, saveSetting, refit, runStudy,
    report,
  };
}
