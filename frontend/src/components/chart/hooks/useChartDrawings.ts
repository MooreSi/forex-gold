import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { Drawing, DrawingKind, DrawingPoint } from "../internal/drawingGeometry";

export type DrawingTool = DrawingKind | null;

/**
 * The Broker chart's drawings for one symbol, and the tool in hand
 * (docs/todo/011).
 *
 * Read once when the chart opens, not polled: drawings change only when the
 * person looking at the chart changes them, and every change here updates
 * the list from the server's answer. A failed save leaves the list as the
 * server has it and says so, rather than drawing something that is not
 * stored and would vanish on the next load, which is the bug this exists
 * to fix.
 */
export function useChartDrawings(symbol: string) {
  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const [tool, setTool] = useState<DrawingTool>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.get<Drawing[]>(`/api/chart/drawings?symbol=${encodeURIComponent(symbol)}`)
      .then((rows) => { if (live) setDrawings(Array.isArray(rows) ? rows : []); })
      .catch((e: Error) => { if (live) setError(`Could not load drawings: ${e.message}`); });
    return () => { live = false; };
  }, [symbol]);

  const create = useCallback(async (kind: DrawingKind, points: DrawingPoint[]) => {
    try {
      const saved = await api.post<Drawing>("/api/chart/drawings", { symbol, kind, points });
      setDrawings((ds) => [...ds, saved]);
      setError(null);
    } catch (e) {
      setError(`Not saved: ${(e as Error).message}`);
    }
  }, [symbol]);

  const move = useCallback(async (id: number, points: DrawingPoint[]) => {
    try {
      const saved = await api.put<Drawing>(`/api/chart/drawings/${id}`, { points });
      setDrawings((ds) => ds.map((d) => (d.id === id ? saved : d)));
      setError(null);
    } catch (e) {
      setError(`Move not saved: ${(e as Error).message}`);
    }
  }, []);

  const remove = useCallback(async (id: number) => {
    try {
      await api.del(`/api/chart/drawings/${id}`);
      setDrawings((ds) => ds.filter((d) => d.id !== id));
      setSelectedId((s) => (s === id ? null : s));
      setError(null);
    } catch (e) {
      setError(`Not deleted: ${(e as Error).message}`);
    }
  }, []);

  return useMemo(() => ({
    drawings, tool, setTool, selectedId, setSelectedId, error, create, move, remove,
  }), [drawings, tool, selectedId, error, create, move, remove]);
}

export type ChartDrawings = ReturnType<typeof useChartDrawings>;
