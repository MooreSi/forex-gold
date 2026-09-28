import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import type { IChartApi, ISeriesApi, Logical } from "lightweight-charts";
import { chartColours } from "@/components/shared/chartTheme";
import type { ChartDrawings } from "../hooks/useChartDrawings";
import {
  logicalToTime, POINTS_PER_KIND, timeToLogical, type Drawing, type DrawingPoint,
} from "./drawingGeometry";
import { DrawingShape } from "./DrawingShape";

interface DrawingLayerProps {
  chart: IChartApi;
  series: ISeriesApi<"Candlestick">;
  /** True once the chart has been removed; every chart call is then off. */
  isDisposed: () => boolean;
  times: number[];
  tfSeconds: number;
  d: ChartDrawings;
}

type Drag =
  | { mode: "handle"; drawing: Drawing; index: number }
  | { mode: "body"; drawing: Drawing; from: { x: number; y: number } };

/**
 * The drawings over the Broker chart, and the mouse work that makes them
 * (docs/todo/011). An SVG over lightweight-charts' canvas, like the FVG
 * layer: the library has no drawing primitives.
 *
 * With no tool in hand the layer lets the mouse through to the chart (pan,
 * zoom) everywhere except on a drawing. With a tool in hand a transparent
 * surface takes the clicks, and the tool drops back to the pointer once the
 * drawing is placed.
 */
export function DrawingLayer({ chart, series, isDisposed, times, tfSeconds, d }: DrawingLayerProps) {
  const svg = useRef<SVGSVGElement>(null);
  const [, setFrame] = useState(0);
  const [pending, setPending] = useState<DrawingPoint[]>([]);
  const [hover, setHover] = useState<DrawingPoint | null>(null);
  const [draft, setDraftState] = useState<{ id: number; points: DrawingPoint[] } | null>(null);
  // Mirrored in a ref so mouse-up can read the last position without a
  // side effect inside a state updater (React may run those twice).
  const draftRef = useRef(draft);
  const setDraft = (v: typeof draft) => { draftRef.current = v; setDraftState(v); };
  const drag = useRef<Drag | null>(null);

  // Re-project on every pan and zoom: an anchor is a (time, price), and its
  // pixels are only true for the view they were computed in.
  useEffect(() => {
    const redraw = () => setFrame((n) => n + 1);
    // A click on the chart itself (not on a drawing) lets go of the selection.
    const deselect = () => d.setSelectedId(null);
    const scale = chart.timeScale();
    scale.subscribeVisibleLogicalRangeChange(redraw);
    chart.subscribeCrosshairMove(redraw);
    chart.subscribeClick(deselect);
    return () => {
      if (isDisposed()) return;
      scale.unsubscribeVisibleLogicalRangeChange(redraw);
      chart.unsubscribeCrosshairMove(redraw);
      chart.unsubscribeClick(deselect);
    };
  }, [chart, isDisposed, d.setSelectedId]);

  const local = (e: { clientX: number; clientY: number }) => {
    const r = svg.current?.getBoundingClientRect();
    return { x: e.clientX - (r?.left ?? 0), y: e.clientY - (r?.top ?? 0) };
  };

  const toPoint = useCallback((x: number, y: number): DrawingPoint | null => {
    if (isDisposed()) return null;
    const l = chart.timeScale().coordinateToLogical(x);
    const price = series.coordinateToPrice(y);
    if (l === null || price === null) return null;
    return {
      time: Math.round(logicalToTime(times, tfSeconds, l)),
      price: Number(price.toFixed(5)),
    };
  }, [chart, series, isDisposed, times, tfSeconds]);

  const project = useCallback((p: DrawingPoint) => {
    if (isDisposed()) return null;
    // lightweight-charts answers 0 for a fractional index (v4.2.3's
    // indexToCoordinate returns 0 unless isInteger), and a time between two
    // bars IS fractional: a 5m line viewed on 1H. The mapping is linear, so
    // interpolate between the two whole bars either side.
    const l = timeToLogical(times, tfSeconds, p.time);
    const scale = chart.timeScale();
    const i = Math.floor(l);
    const a = scale.logicalToCoordinate(i as Logical);
    const b = l === i ? a : scale.logicalToCoordinate((i + 1) as Logical);
    const x = a === null || b === null ? null : a + (l - i) * (b - a);
    const y = series.priceToCoordinate(p.price);
    return x === null || y === null ? null : { x, y };
  }, [chart, series, isDisposed, times, tfSeconds]);

  const priceToY = useCallback(
    (price: number) => (isDisposed() ? null : series.priceToCoordinate(price)),
    [series, isDisposed],
  );

  const cancel = useCallback(() => {
    setPending([]);
    setHover(null);
    d.setTool(null);
  }, [d]);

  // Escape abandons; Delete removes the selected drawing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement
        && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName);
      // A dialog over the chart (an order from a selected position) owns
      // its keys: Delete there must not delete the drawing behind it.
      const inDialog = e.target instanceof HTMLElement && e.target.closest('[role="dialog"]');
      if (typing || inDialog) return;
      if (e.key === "Escape") {
        cancel();
        d.setSelectedId(null);
      } else if ((e.key === "Delete" || e.key === "Backspace") && d.selectedId !== null) {
        void d.remove(d.selectedId);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cancel, d]);

  const place = (e: ReactMouseEvent) => {
    if (!d.tool) return;
    const { x, y } = local(e);
    const pt = toPoint(x, y);
    if (!pt) return;
    const points = [...pending, pt];
    if (points.length < POINTS_PER_KIND[d.tool]) {
      setPending(points);
      return;
    }
    void d.create(d.tool, points);
    cancel();
  };

  const shifted = (from: DrawingPoint[], dx: number, dy: number): DrawingPoint[] | null => {
    const dl = (chart.timeScale().coordinateToLogical(dx) ?? 0)
      - (chart.timeScale().coordinateToLogical(0) ?? 0);
    const p0 = series.coordinateToPrice(0);
    const p1 = series.coordinateToPrice(dy);
    if (p0 === null || p1 === null) return null;
    return from.map((p) => ({
      time: Math.round(logicalToTime(times, tfSeconds, timeToLogical(times, tfSeconds, p.time) + dl)),
      price: Number((p.price + (p1 - p0)).toFixed(5)),
    }));
  };

  // Dragging runs on window listeners so it follows the mouse off the shape.
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const g = drag.current;
      if (!g || isDisposed()) return;
      const { x, y } = local(e);
      if (g.mode === "handle") {
        const pt = toPoint(x, y);
        if (!pt) return;
        const points = g.drawing.points.map((p, i) => (i === g.index ? pt : p));
        setDraft({ id: g.drawing.id, points });
      } else {
        const points = shifted(g.drawing.points, x - g.from.x, y - g.from.y);
        if (points) setDraft({ id: g.drawing.id, points });
      }
    };
    const onUp = () => {
      const g = drag.current;
      const cur = draftRef.current;
      drag.current = null;
      if (!g) return;
      setDraft(null);
      if (cur && JSON.stringify(cur.points) !== JSON.stringify(g.drawing.points)) {
        void d.move(cur.id, cur.points);
      }
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  });

  const onBodyDown = (e: ReactMouseEvent, drawing: Drawing) => {
    e.stopPropagation();
    d.setSelectedId(drawing.id);
    drag.current = { mode: "body", drawing, from: local(e) };
  };
  const onHandleDown = (e: ReactMouseEvent, drawing: Drawing, index: number) => {
    e.stopPropagation();
    drag.current = { mode: "handle", drawing, index };
  };

  if (isDisposed()) return null;
  const colours = chartColours();
  const width = svg.current?.clientWidth ?? 0;
  const preview = d.tool && pending.length > 0 && hover
    ? { id: -1, symbol: "", kind: d.tool, points: [...pending, hover] } as Drawing
    : null;

  return (
    <svg
      ref={svg}
      data-testid="drawings-overlay"
      className="pointer-events-none absolute inset-0 z-20 h-full w-full"
    >
      {d.drawings.map((dr) => (
        <DrawingShape
          key={dr.id}
          drawing={draft?.id === dr.id ? { ...dr, points: draft.points } : dr}
          project={project}
          priceToY={priceToY}
          width={width}
          colour={d.selectedId === dr.id ? colours.accent : colours.remote}
          selected={d.selectedId === dr.id}
          onBodyDown={onBodyDown}
          onHandleDown={onHandleDown}
        />
      ))}
      {preview && (
        <DrawingShape drawing={preview} project={project} priceToY={priceToY} width={width}
          colour={colours.accent} selected={false}
          onBodyDown={() => undefined} onHandleDown={() => undefined} />
      )}
      {d.tool && (
        <rect
          data-testid="drawing-surface"
          x={0} y={0} width="100%" height="100%"
          fill="transparent" pointerEvents="all" style={{ cursor: "crosshair" }}
          onMouseDown={place}
          onMouseMove={(e) => { const { x, y } = local(e); setHover(toPoint(x, y)); }}
        />
      )}
    </svg>
  );
}
