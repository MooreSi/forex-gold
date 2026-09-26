import { useCallback, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { CheckResult, Passive } from "../internal/latency_rows";
import { useSettingsResource } from "./useSettingsResource";

/**
 * Settings > Latency: the passive read, and the Run check button.
 *
 * The check is a POST and never polled: it reaches Telegram, the bridge, the
 * EA and the VPS once each. It places, modifies and closes nothing.
 */
export function useLatencyController() {
  const passive = useSettingsResource<Passive>("/api/settings/latency");
  const [result, setResult] = useState<CheckResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runCheck = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      setResult(await api.post<CheckResult>("/api/settings/latency/check", {}));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRunning(false);
    }
    await passive.reload();
  }, [passive]);

  // A check carries the same traces, fresher; otherwise the passive read.
  const pipelines = result?.local.pipelines ?? passive.data?.pipelines;
  const structural = result?.local.structural ?? passive.data?.structural ?? [];

  return {
    loading: !passive.data && !passive.error,
    loadError: passive.error,
    paired: passive.data?.paired ?? false,
    pipelines,
    structural,
    result,
    running,
    error,
    runCheck,
  };
}
