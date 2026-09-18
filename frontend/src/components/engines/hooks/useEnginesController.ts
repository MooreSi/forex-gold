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
  settings: Record<string, unknown>;
  pro_model: Record<string, unknown>;
}

export function useEnginesController() {
  const [busy, setBusy] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);

  const state = usePoll<EnginesState>(
    "engines/state",
    useCallback(() => api.get<EnginesState>("/api/engines/state"), []),
    5_000,
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
      setReport(body.report);
    } catch (e) {
      setRefusal(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  return {
    state,
    engines: asArray<EngineRow>(state.data?.engines),
    busy, refusal, report, setRunning, saveSetting, refit, runStudy,
  };
}
