import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType, createChart, CrosshairMode,
  type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { Candle, Overlays, Tick, Trade } from "@/api/types";
import { chartColours, watchTheme } from "@/components/shared/chartTheme";
import { rectsFor, type FvgRect } from "./internal/fvgGeometry";
import { FvgOverlay } from "./internal/FvgOverlay";
import { liveBar, type Bar } from "./internal/liveBar";
import { DrawingLayer } from "./internal/DrawingLayer";
import { DrawingToolbar } from "./internal/DrawingToolbar";
import type { ChartDrawings } from "./hooks/useChartDrawings";

interface CandleChartProps {
  candles: Candle[];
  overlays: Overlays | null;
  tick: Tick | null;
  trades: Trade[];
  /** Seconds per bar. With it, each tick moves the forming candle. */
  timeframeSeconds?: number;
  /** The drawings and tools (docs/todo/011). Without them, no drawing layer. */
  drawings?: ChartDrawings;
}

// The colours the NiceGUI chart used, kept so the two look like the same app.
// Green is a rising candle and red a falling one — the same profit/loss
// semantics the rest of the UI uses, which is why they are not chosen freely.
const BULL = "#00cc88";
const BEAR = "#ff4444";
const EMA_COLOURS: Record<string, string> = {
  "9": "#ffd700",   // gold — fastest
  "21": "#ff9900",  // orange
  "50": "#64b4ff",  // sky blue — slowest
};

/**
 * The candle canvas. Imperative by necessity: lightweight-charts owns its own
 * DOM, so this component creates the chart once and pushes data into it on
 * every change rather than re-rendering.
 */
export function CandleChart({
  candles, overlays, tick, trades, timeframeSeconds, drawings,
}: CandleChartProps) {
  const holder = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candleSeries = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const emaSeries = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  // The bar drawn last: the newest candle, then whatever the ticks made of it.
  const lastBar = useRef<Bar | null>(null);
  const [fvgRects, setFvgRects] = useState<FvgRect[]>([]);
  // lightweight-charts disposes hard: every method on a removed chart, and on
  // its time scale, throws "Object is disposed". React runs effect cleanups in
  // DEFINITION order, so the effect below that creates the chart tears it down
  // BEFORE the subscription effect further down gets to unsubscribe -- and
  // that unsubscribe then threw on every unmount. This is how the later
  // cleanup knows the object it holds is already gone.
  const disposed = useRef(false);
  const [themeTick, setThemeTick] = useState(0);
  // The chart and series as state, not only refs, so the drawing layer
  // renders once they exist and goes when they do.
  const [apis, setApis] = useState<{
    chart: IChartApi; series: ISeriesApi<"Candlestick">;
  } | null>(null);
  const isDisposed = useCallback(() => disposed.current, []);
  const times = useMemo(() => candles.map((c) => c.ts), [candles]);

  // The document attribute rather than `useTheme()`. This component must be
  // mountable anywhere -- a chart that throws because a context is missing is
  // a blank dashboard over a colour, and the theme is the least important
  // thing on it.
  useEffect(() => watchTheme(() => setThemeTick((n) => n + 1)), []);

  useEffect(() => {
    if (!holder.current) return;
    const colours = chartColours();
    const c = createChart(holder.current, {
      layout: {
        background: { type: ColorType.Solid, color: colours.background },
        textColor: colours.text,
        fontFamily: "ui-monospace, SF Mono, Menlo, monospace",
      },
      grid: {
        vertLines: { color: colours.grid },
        horzLines: { color: colours.grid },
      },
      rightPriceScale: { borderColor: colours.border },
      timeScale: { borderColor: colours.border, timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: true,
    });
    chart.current = c;
    candleSeries.current = c.addCandlestickSeries({
      upColor: BULL, downColor: BEAR, borderVisible: false,
      wickUpColor: BULL, wickDownColor: BEAR,
    });
    disposed.current = false;
    setApis({ chart: c, series: candleSeries.current });
    return () => {
      disposed.current = true;
      setApis(null);
      c.remove();
      chart.current = null;
      candleSeries.current = null;
      emaSeries.current.clear();
    };
  }, []);

  // Repaint on a theme change. The chart is created once and would otherwise
  // keep whichever theme was in force at that moment.
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    const colours = chartColours();
    if (typeof c.applyOptions !== "function") return;
    c.applyOptions({
      layout: {
        background: { type: ColorType.Solid, color: colours.background },
        textColor: colours.text,
      },
      grid: {
        vertLines: { color: colours.grid },
        horzLines: { color: colours.grid },
      },
      rightPriceScale: { borderColor: colours.border },
      timeScale: { borderColor: colours.border },
    });
  }, [themeTick]);

  useEffect(() => {
    if (!candleSeries.current) return;
    const bars = candles.map((c) => ({
      time: c.ts, open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    candleSeries.current.setData(bars.map((b) => ({ ...b, time: b.time as UTCTimestamp })));
    lastBar.current = bars[bars.length - 1] ?? null;
  }, [candles]);

  // The forming candle, moved by each tick between candle refreshes. Defined
  // after the effect above so a refresh's setData lands first.
  useEffect(() => {
    const series = candleSeries.current;
    const prev = lastBar.current;
    if (disposed.current || !series || !prev || !tick || !timeframeSeconds) return;
    const next = liveBar(prev, tick, timeframeSeconds);
    if (!next) return;
    series.update({ ...next, time: next.time as UTCTimestamp });
    lastBar.current = next;
  }, [tick, candles, timeframeSeconds]);

  useEffect(() => {
    if (!chart.current || !overlays) return;
    for (const [period, values] of Object.entries(overlays.emas)) {
      let series = emaSeries.current.get(period);
      if (!series) {
        series = chart.current.addLineSeries({
          color: EMA_COLOURS[period] ?? "#9ca3af",
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          title: `EMA ${period}`,
        });
        emaSeries.current.set(period, series);
      }
      series.setData(
        values
          .map((v, i) => ({ time: candles[i]?.ts as UTCTimestamp, value: v }))
          .filter((p): p is { time: UTCTimestamp; value: number } =>
            p.time !== undefined && p.value !== null && Number.isFinite(p.value)),
      );
    }
  }, [overlays, candles]);

  useEffect(() => {
    const series = candleSeries.current;
    if (!series) return;
    const lastTs = candles[candles.length - 1]?.ts;
    if (lastTs === undefined) return;
    const markers: SeriesMarker<Time>[] = trades
      .filter((t) => typeof t.entry === "number")
      .map((t) => ({
        time: lastTs as UTCTimestamp,
        position: t.direction === "SELL" ? "aboveBar" : "belowBar",
        color: t.direction === "SELL" ? BEAR : BULL,
        shape: t.direction === "SELL" ? "arrowDown" : "arrowUp",
        text: `${String(t.direction ?? "")} ${String(t.entry ?? "")}`,
      }));
    series.setMarkers(markers);
  }, [trades, candles]);

  useEffect(() => {
    const series = candleSeries.current;
    if (!series || !tick) return;
    // Bid and ask as price lines, the way the NiceGUI chart drew them. SL/TP
    // lines are deliberately absent: they were removed on 2026-08-04 because
    // they buried the price action, and the numbers live on the trades panel.
    const bid = series.createPriceLine({
      price: tick.bid, color: "rgba(0,204,136,0.7)", lineWidth: 1,
      lineStyle: 2, axisLabelVisible: true, title: "Bid",
    });
    const ask = series.createPriceLine({
      price: tick.ask, color: "rgba(100,180,255,0.6)", lineWidth: 1,
      lineStyle: 2, axisLabelVisible: true, title: "Ask",
    });
    return () => {
      // Guarded for the reason `disposed` exists, one effect up. This cleanup
      // runs AFTER the chart has been removed, and removing a price line from
      // a disposed series does not throw here -- it reaches the model, the
      // model queues a repaint, and a frame later that repaint throws
      // "Object is disposed" with no application frame in its stack. Three of
      // them per unmount, seen in the console on 2026-09-22.
      if (disposed.current) return;
      series.removePriceLine(bid);
      series.removePriceLine(ask);
    };
  }, [tick]);

  // ── Fair-value gaps ────────────────────────────────────────────────────────
  // lightweight-charts has no rectangle primitive, so the zones are an SVG
  // layer over its canvas, positioned through the chart's own coordinate
  // conversions. Recomputed whenever the chart is panned, zoomed or resized —
  // a band left at stale pixels is a price level that is not there.
  const redrawFvgs = useCallback(() => {
    const c = chart.current;
    const series = candleSeries.current;
    const box = holder.current;
    if (disposed.current || !c || !series || !box) return setFvgRects([]);

    const zones = overlays?.fvgs ?? [];
    if (zones.length === 0) return setFvgRects([]);

    const range = c.timeScale().getVisibleRange();
    if (!range) return setFvgRects([]);

    setFvgRects(rectsFor(zones, {
      timeToX: (ts) => c.timeScale().timeToCoordinate(ts as UTCTimestamp),
      priceToY: (price) => series.priceToCoordinate(price),
      visibleTo: Number(range.to),
      width: box.clientWidth,
      height: box.clientHeight,
    }));
  }, [overlays]);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    redrawFvgs();
    const scale = c.timeScale();
    scale.subscribeVisibleTimeRangeChange(redrawFvgs);
    return () => {
      if (disposed.current) return;
      scale.unsubscribeVisibleTimeRangeChange(redrawFvgs);
    };
  }, [redrawFvgs, candles]);

  return (
    <div ref={holder} data-testid="candle-chart" className="relative h-full w-full">
      {drawings && apis && timeframeSeconds && (
        <>
          <DrawingToolbar d={drawings} />
          <DrawingLayer
            chart={apis.chart} series={apis.series} isDisposed={isDisposed}
            times={times} tfSeconds={timeframeSeconds} d={drawings}
          />
        </>
      )}
      <FvgOverlay rects={fvgRects} themeTick={themeTick} />
    </div>
  );
}
