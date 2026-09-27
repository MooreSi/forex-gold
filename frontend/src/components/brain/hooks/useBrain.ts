import { useCallback } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import type { BrainEvent, BrainGate } from "../content/brainLayout";

export interface BrainSnapshot {
  now: number;
  gates: BrainGate[];
  events: BrainEvent[];
}

/** The brain's one read, every 3 seconds while it is on screen. */
export function useBrain() {
  return usePoll<BrainSnapshot>(
    "brain",
    useCallback(() => api.get<BrainSnapshot>("/api/brain"), []),
    3_000,
  );
}
