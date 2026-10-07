import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { Candle } from "@/api/types";
import { HISTORY_PAGE, mergeHistory } from "../internal/history";

/**
 * Bars older than the live window, fetched a page at a time when the chart is
 * panned to its left edge (docs/todo/011 phase 2).
 *
 * One request at a time, and none after the broker answers with nothing older:
 * the edge event fires on every frame of a drag. A new timeframe starts over.
 *
 * Once history is held, bars that slide out of the live window as it moves on
 * are kept here, so a long session does not open a gap between the two.
 */
export function useChartHistory(timeframe: string, live: Candle[]) {
  const [older, setOlder] = useState<Candle[]>([]);
  const busy = useRef(false);
  const exhausted = useRef(false);
  const current = useRef(timeframe);
  const oldest = useRef<number | undefined>(undefined);
  const prevLive = useRef<Candle[]>([]);

  useEffect(() => {
    current.current = timeframe;
    busy.current = false;
    exhausted.current = false;
    prevLive.current = [];
    setOlder([]);
  }, [timeframe]);

  useEffect(() => {
    const before = prevLive.current;
    prevLive.current = live;
    if (before.length === 0 || live.length === 0) return;
    setOlder((p) => (p.length === 0 ? p : mergeHistory(mergeHistory(p, before), live).filter(
      (c) => c.ts < live[0].ts)));
  }, [live]);

  oldest.current = older[0]?.ts ?? live[0]?.ts;

  const loadOlder = useCallback(async () => {
    const before = oldest.current;
    if (busy.current || exhausted.current || before === undefined) return;
    busy.current = true;
    const tf = timeframe;
    try {
      const rows = await api.get<Candle[]>(
        `/api/chart/history?timeframe=${tf}&before=${before}&count=${HISTORY_PAGE}`);
      if (current.current !== tf) return;
      if (!Array.isArray(rows) || rows.length === 0) {
        exhausted.current = true;
        return;
      }
      setOlder((p) => mergeHistory(rows, p));
    } catch {
      // Left as it was; the next visit to the edge asks again.
    } finally {
      if (current.current === tf) busy.current = false;
    }
  }, [timeframe]);

  return { older, loadOlder };
}
