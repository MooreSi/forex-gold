import { useCallback, useMemo } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";
import { strategyChoices } from "../internal/orderStrategy";

/**
 * The strategies the order dialogs offer. Shares the "trading/strategies"
 * poll with the Strategy screen, so it adds no request while that is open.
 * Read by the dialogs' owners, not the dialogs: a dialog that fetched on
 * mount would put a GET ahead of the order in every caller's request log.
 */
export function useStrategyChoices() {
  const poll = usePoll<{ catalogue: unknown }>(
    "trading/strategies",
    useCallback(() => api.get<{ catalogue: unknown }>("/api/trading/strategies"), []),
    300_000,
  );
  return useMemo(
    () => strategyChoices(asArray<{ key: string; label: string; kind: string }>(
      poll.data?.catalogue)),
    [poll.data],
  );
}
