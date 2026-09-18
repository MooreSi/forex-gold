/**
 * One interval, many subscribers.
 *
 * The naive shape of a live dashboard is ten components each running their own
 * `setInterval` against their own endpoint. The NiceGUI app had roughly twenty
 * `ui.timer()` calls and the cost was real: every timer ran on the server's
 * event loop for every connected client, and an unattended browser tab on the
 * VPS was directly implicated in event-loop stalls.
 *
 * So this is the only polling primitive in the app:
 *
 * - one timer per key, however many components subscribe;
 * - an in-flight request is never duplicated by a tick that arrives while it
 *   is still running;
 * - polling pauses while the document is hidden, because a browser throttles a
 *   background tab anyway and what it does not throttle is the server work.
 *
 * A new "what is the latest X" need becomes an extra field on an existing
 * consolidated response — assembled server-side where it is one cheap read —
 * not a new key here.
 */
import { useCallback, useEffect, useRef, useState } from "react";

interface PollEntry<T> {
  fetcher: () => Promise<T>;
  intervalMs: number;
  subscribers: Set<(state: PollState<T>) => void>;
  state: PollState<T>;
  timer: ReturnType<typeof setInterval> | null;
  inFlight: Promise<void> | null;
}

export interface PollState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  /** Epoch ms of the last successful response. Null until the first one. */
  updatedAt: number | null;
}

const registry = new Map<string, PollEntry<unknown>>();

function emit<T>(entry: PollEntry<T>) {
  entry.subscribers.forEach((s) => s(entry.state));
}

function runOnce<T>(entry: PollEntry<T>): Promise<void> {
  // Dedup: a tick that lands while a request is still open joins it instead of
  // starting a second one. Without this a slow endpoint and a fast interval
  // queue up requests faster than they complete.
  if (entry.inFlight) return entry.inFlight;

  entry.state = { ...entry.state, loading: true };
  emit(entry);

  const promise = entry
    .fetcher()
    .then((data) => {
      entry.state = { data, error: null, loading: false, updatedAt: Date.now() };
    })
    .catch((error: Error) => {
      // The previous data is kept deliberately. A transient failure should
      // show a stale number flagged as stale, not blank the panel — but the
      // staleness must be visible, which is what `updatedAt` is for.
      entry.state = { ...entry.state, error, loading: false };
    })
    .finally(() => {
      entry.inFlight = null;
      emit(entry);
    });

  entry.inFlight = promise;
  return promise;
}

function startTimer<T>(entry: PollEntry<T>) {
  if (entry.timer !== null) return;
  entry.timer = setInterval(() => {
    if (typeof document !== "undefined" && document.hidden) return;
    void runOnce(entry);
  }, entry.intervalMs);
}

function stopTimer<T>(entry: PollEntry<T>) {
  if (entry.timer === null) return;
  clearInterval(entry.timer);
  entry.timer = null;
}

/** Test seam. Leaving a live interval behind between tests is how one test's
 *  fetch shows up as another's failure. */
export function resetPolls(): void {
  registry.forEach((entry) => stopTimer(entry));
  registry.clear();
}

export function usePoll<T>(
  key: string,
  fetcher: () => Promise<T>,
  intervalMs = 5000,
): PollState<T> & { refresh: () => Promise<void> } {
  const existing = registry.get(key) as PollEntry<T> | undefined;
  const entryRef = useRef<PollEntry<T>>(
    existing ?? {
      fetcher,
      intervalMs,
      subscribers: new Set(),
      state: { data: null, error: null, loading: false, updatedAt: null },
      timer: null,
      inFlight: null,
    },
  );
  if (!existing) registry.set(key, entryRef.current as PollEntry<unknown>);
  const entry = registry.get(key) as PollEntry<T>;
  // The newest fetcher wins, so a changed query string is picked up on the
  // next tick without tearing the interval down and restarting it.
  entry.fetcher = fetcher;

  const [state, setState] = useState<PollState<T>>(entry.state);

  useEffect(() => {
    entry.subscribers.add(setState);
    setState(entry.state);
    // Populate before the first tick, exactly as the NiceGUI pages did with
    // `ensure_future(_refresh())` beside `ui.timer(n, _refresh)`. Without it
    // the panel is empty for a whole interval on every page load.
    if (entry.state.updatedAt === null) void runOnce(entry);
    startTimer(entry);
    return () => {
      entry.subscribers.delete(setState);
      if (entry.subscribers.size === 0) stopTimer(entry);
    };
  }, [entry]);

  useEffect(() => {
    const onVisible = () => {
      if (!document.hidden) void runOnce(entry);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [entry]);

  const refresh = useCallback(() => runOnce(entry), [entry]);
  return { ...state, refresh };
}
