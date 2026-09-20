import { useCallback, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { UpdateStatus } from "@/api/types";

/**
 * The pending GitHub update: what it is, and applying it.
 *
 * Shared by the header's popup and Settings > Node & Updates, because the two
 * screens answer the same question and must never disagree about it.
 *
 * Deliberately NOT a poll. A check runs `git fetch`, which takes as long as
 * the network takes; whether an update exists already rides the header's one
 * shared poll (`/api/system/header`), from a cache on the backend. This hook
 * is what runs when someone asks — the Check button, or opening the popup.
 *
 * `includeSummary` is money. The plain-English lines cost a paid model call,
 * so only the popup that shows the operator what they are about to install
 * asks for them; the Check button does not.
 */
export function useUpdateCheck(includeSummary = false) {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState("");
  const [applied, setApplied] = useState(false);

  const check = useCallback(async () => {
    setChecking(true);
    setError(null);
    try {
      setStatus(await api.get<UpdateStatus>(
        `/api/node/update?include_summary=${includeSummary}`,
      ));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      // Always cleared. A Check button stuck on "Checking..." is
      // indistinguishable from the one that did nothing, which is what this
      // screen was reported for.
      setChecking(false);
    }
  }, [includeSummary]);

  const apply = useCallback(async () => {
    setApplying(true);
    setApplyError("");
    setApplied(false);
    try {
      // `/api/node/update/apply`. POSTing to `/api/node/update` — which the
      // Settings button did until 2026-09-20 — reaches no route at all, so
      // the button appeared to work and updated nothing.
      const body = await api.post<{ result?: { ok?: boolean; error?: string } }>(
        "/api/node/update/apply", {},
      );
      const result = body?.result ?? {};
      if (result.ok === true) {
        setApplied(true);
      } else {
        // A failed update that reports success is the dangerous outcome: the
        // operator restarts into the old code believing it updated.
        setApplyError(String(result.error || "The update did not complete."));
      }
    } catch (e) {
      setApplyError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setApplying(false);
    }
  }, []);

  return { status, checking, error, check, applying, applyError, applied, apply };
}
