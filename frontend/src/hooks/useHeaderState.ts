import { api } from "@/api/client";
import { usePoll } from "./usePoll";
import type { HeaderState } from "@/api/types";

/**
 * The one consolidated poll the whole shell shares.
 *
 * Account, bridge health, tick, active trader, halt reason and the remote link
 * arrive together from `/api/system/header`, on the 5s cadence the NiceGUI app
 * used for account and positions. A new "is X live" need becomes a field on
 * that response, not a second interval here — see usePoll's docstring for why
 * that rule exists.
 */
export function useHeaderState() {
  return usePoll<HeaderState>(
    "system/header",
    () => api.get<HeaderState>("/api/system/header"),
    5000,
  );
}
