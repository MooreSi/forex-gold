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
import { useCallback, useEffect, useState } from "react";

type Subscriber<T> = (state: PollState<T>) => void;

interface PollEntry<T> {
  fetcher: () => Promise<T>;
  /**
   * The cadence currently running: the SHORTEST any live subscriber asked
   * for. Derived from `subscribers`, never set directly — see `syncTimer`.
   */
  intervalMs: number;
  /**
   * Subscriber -> the interval THAT subscriber asked for.
   *
   * A Map rather than a Set because the interval belongs to the subscriber,
   * not to the key. It used to belong to the key, fixed by whoever created
   * the entry first, which made the cadence depend on the order tabs were
   * visited in. See `syncTimer`.
   */
  subscribers: Map<Subscriber<T>, number>;
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
  entry.subscribers.forEach((_interval, listener) => listener(entry.state));
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

/**
 * Run at the shortest cadence any live subscriber asked for.
 *
 * The interval used to be fixed when the entry was created and every later
 * subscriber's was ignored, so the cadence of a shared key depended on which
 * tab had been opened first. The Dashboard asking for a 3s refresh of the
 * open positions got 5s if Trading had been visited before it, and Trading
 * silently got 3s if it had not — neither of them visible anywhere.
 *
 * It falls back when the fast subscriber leaves, deliberately: this key
 * reaches the MT5 bridge uncached, and one visit to a fast screen must not
 * leave an endpoint polled harder than anybody is asking for it (bugs/030 is
 * what that costs).
 */
function syncTimer<T>(entry: PollEntry<T>) {
  if (entry.subscribers.size === 0) {
    stopTimer(entry);
    return;
  }
  const wanted = Math.min(...entry.subscribers.values());
  if (entry.timer !== null && wanted === entry.intervalMs) return;
  stopTimer(entry);
  entry.intervalMs = wanted;
  startTimer(entry);
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

function entryFor<T>(
  key: string, fetcher: () => Promise<T>, intervalMs: number,
): PollEntry<T> {
  const existing = registry.get(key) as PollEntry<T> | undefined;
  if (existing) return existing;
  const entry: PollEntry<T> = {
    fetcher,
    intervalMs,
    subscribers: new Map(),
    state: { data: null, error: null, loading: false, updatedAt: null },
    timer: null,
    inFlight: null,
  };
  registry.set(key, entry as PollEntry<unknown>);
  return entry;
}

export function usePoll<T>(
  key: string,
  fetcher: () => Promise<T>,
  intervalMs = 5000,
): PollState<T> & { refresh: () => Promise<void> } {
  // Looked up by key on EVERY render, not held in a ref.
  //
  // The first version kept the entry in `useRef`, which initialises once — so
  // when the key changed (the Chart tab's timeframe, the Analysis tab's
  // window) the hook registered the OLD entry under the NEW key, the effect's
  // dependency never changed, and no fetch happened. The panel went on showing
  // the previous window's data with no error anywhere. Found by
  // `HistoryPanel.test.tsx > asks again for a different window`.
  const entry = entryFor(key, fetcher, intervalMs);

  const [state, setState] = useState<PollState<T>>(entry.state);

  // The newest fetcher wins, so a changed query string is picked up on the
  // next tick without tearing the interval down and restarting it.
  entry.fetcher = fetcher;

  useEffect(() => {
    entry.subscribers.set(setState, intervalMs);
    setState(entry.state);
    // Populate before the first tick, exactly as the NiceGUI pages did with
    // `ensure_future(_refresh())` beside `ui.timer(n, _refresh)`. Without it
    // the panel is empty for a whole interval on every page load.
    if (entry.state.updatedAt === null) void runOnce(entry);
    syncTimer(entry);
    return () => {
      entry.subscribers.delete(setState);
      // Not just "stop if nobody is left": a fast subscriber leaving must
      // hand the key back to the slower ones rather than keep their cadence.
      syncTimer(entry);
    };
  }, [entry, intervalMs]);

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
