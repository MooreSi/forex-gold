import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";

export interface SettingsResource<T> {
  data: T | null;
  error: string | null;
  saving: boolean;
  /**
   * Bumped on every completed save.
   *
   * A field needs it because "the backend rejected your number" and "the
   * backend agreed with what was already stored" produce the SAME value, so a
   * field that re-syncs only when the value changes keeps the rejected input
   * in the box. Typing 99 into a risk field that stays clamped at 2 left "99"
   * on screen — which reads as a 99% risk setting the engine is not using.
   */
  version: number;
  save: (body: unknown, method?: "PUT" | "POST") => Promise<void>;
  reload: () => Promise<void>;
}

/**
 * One settings domain: read it, write it, show what came back.
 *
 * Not `usePoll`. Settings are not live data — polling them would fight the
 * operator's own typing, and a tick landing mid-edit would put the stored value
 * back into a field they were halfway through changing.
 *
 * `save` always adopts the RESPONSE, never the request. The services clamp and
 * normalise, so what was typed and what the engine will use are not always the
 * same number, and the field has to show the second one.
 */
export function useSettingsResource<T>(path: string): SettingsResource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [version, setVersion] = useState(0);

  const reload = useCallback(async () => {
    try {
      setData(await api.get<T>(path));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [path]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const save = useCallback(
    async (body: unknown, method: "PUT" | "POST" = "PUT") => {
      setSaving(true);
      setError(null);
      try {
        const next = method === "PUT"
          ? await api.put<T>(path, body)
          : await api.post<T>(path, body);
        setData(next);
        setVersion((v) => v + 1);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setSaving(false);
      }
    },
    [path],
  );

  return { data, error, saving, version, save, reload };
}
