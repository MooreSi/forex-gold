import { useCallback, useMemo } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import type { HaltState } from "@/api/types";
import { manualOrderDisabledReason, orderDisabledReason } from "./useTradingController";

/**
 * The halt poll ("trading/halt", shared by key with every other reader) and
 * the answer to "may a manual order be placed now", for the Chart tab's order
 * buttons (2026-09-28). The answer is `orderDisabledReason`, which lives in
 * useTradingController: tests/risk/test_halt_reason_is_visible.py pins that
 * the Trading tab's controller reads the halt reason, and one copy of the
 * rule is the point.
 */
export function useOrderGate() {
  const halt = usePoll<HaltState>(
    "trading/halt",
    useCallback(() => api.get<HaltState>("/api/trading/halt"), []),
    5_000,
  );
  const disabledReason = useMemo(
    () => orderDisabledReason(halt.data, halt.error),
    [halt.data, halt.error],
  );
  const manualDisabledReason = useMemo(
    () => manualOrderDisabledReason(halt.data, halt.error),
    [halt.data, halt.error],
  );
  return { halt, disabledReason, manualDisabledReason };
}
