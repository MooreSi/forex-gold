import { useCallback, useState } from "react";
import { api, ApiError } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import type { SetForgetAutoStatus } from "@/api/types";

const PATH = "/api/trading/setforget/auto";

/**
 * The Auto switch's state. The scan runs server-side every 15 minutes; this
 * only reads its status and flips it. Polled every 30 s so the last decision
 * on screen is at most one wake of the backend loop behind.
 *
 * The PUT's own answer is shown straight away. Then the poll is refreshed
 * TWICE: `usePoll.refresh` joins a request already in flight, so the first
 * call can only settle a read that started before the switch; the second is
 * the first read guaranteed to start after it. Until that read lands the
 * PUT's answer is what shows -- without this a click during the page's first
 * read re-showed that read's stale "off" for a whole 30 s interval.
 */
export function useSetForgetAuto() {
  const poll = usePoll<SetForgetAutoStatus>(
    "setforget-auto",
    useCallback(() => api.get<SetForgetAutoStatus>(PATH), []),
    30_000,
  );
  const [switched, setSwitched] = useState<SetForgetAutoStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const status = switched ?? poll.data;

  const toggle = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const s = await api.put<SetForgetAutoStatus>(PATH, { enabled: !status?.enabled });
      setSwitched(s);
      await poll.refresh();
      await poll.refresh();
      setSwitched(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [status]);

  return { status, busy, error, toggle };
}
