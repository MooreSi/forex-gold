import { useCallback, useEffect, useRef, useState } from "react";
import {
  ColorType, createChart, CrosshairMode,
  type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { Candle, Overlays, Tick, Trade } from "@/api/types";
import { rectsFor, type FvgRect } from "./fvgGeometry";

interface CandleChartProps {
  candles: Candle[];
  overlays: Overlays | null;
  tick: Tick | null;
  trades: Trade[];
}

// The colours the NiceGUI chart used, kept so the two look like the same app.
// Green is a rising candle and red a falling one — the same profit/loss
// semantics the rest of the UI uses, which is why they are not chosen freely.
const BULL = "#00cc88";
const BEAR = "#ff4444";
// A fair-value gap is an imbalance price left behind. Bullish gaps sit below
// price and bearish above, so they take the same profit/loss colours the rest
// of the app uses — at a low alpha, because a zone is context, not a signal.
const FVG_FILL: Record<string, string> = {
  bullish: "rgba(0,204,136,0.13)",
  bearish: "rgba(255,68,68,0.13)",
};
const FVG_EDGE: Record<string, string> = {
  bullish: "rgba(0,204,136,0.45)",
  bearish: "rgba(255,68,68,0.45)",
};

/** A theme token's current value, or a fallback.
 *
 *  lightweight-charts paints to a canvas and cannot use CSS variables, so the
 *  chart has to be TOLD the colours. Until 2026-09-19 it was told #030712
 *  unconditionally, which in light mode is a black rectangle inside a white
 *  panel. */
function token(name: string, fallback: string): string {
  if (typeof getComputedStyle !== "function") return fallback;
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  return value || fallback;
}

function chartColours() {
  return {
    background: token("--color-surface-1", "#030712"),
    text: token("--color-ink-2", "#9ca3af"),
    grid: token("--color-surface-3", "#1b2333"),
    border: token("--color-line", "#263044"),
  };
}

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
export function CandleChart({ candles, overlays, tick, trades }: CandleChartProps) {
  const holder = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candleSeries = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const emaSeries = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const [fvgRects, setFvgRects] = useState<FvgRect[]>([]);
  const [themeTick, setThemeTick] = useState(0);

  // The document attribute rather than `useTheme()`. This component must be
  // mountable anywhere -- a chart that throws because a context is missing is
  // a blank dashboard over a colour, and the theme is the least important
  // thing on it.
  useEffect(() => {
    if (typeof MutationObserver !== "function") return;
    const observer = new MutationObserver(() => setThemeTick((n) => n + 1));
    observer.observe(document.documentElement, {
      attributes: true, attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

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
    return () => {
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
    candleSeries.current.setData(
      candles.map((c) => ({
        time: c.ts as UTCTimestamp,
        open: c.open, high: c.high, low: c.low, close: c.close,
      })),
    );
  }, [candles]);

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
    if (!c || !series || !box) return setFvgRects([]);

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
    return () => scale.unsubscribeVisibleTimeRangeChange(redrawFvgs);
  }, [redrawFvgs, candles]);

  return (
    <div ref={holder} data-testid="candle-chart" className="relative h-full w-full">
      {fvgRects.length > 0 && (
        <svg
          data-testid="fvg-overlay"
          className="pointer-events-none absolute inset-0 h-full w-full"
          aria-hidden
        >
          {fvgRects.map((r) => (
            <rect
              key={`${r.ts}-${r.y}`}
              data-testid={`fvg-${r.direction}-${r.ts}`}
              x={r.x} y={r.y} width={r.width} height={r.height}
              fill={FVG_FILL[r.direction] ?? "rgba(156,163,175,0.10)"}
              stroke={FVG_EDGE[r.direction] ?? "rgba(156,163,175,0.35)"}
              strokeWidth="0.5"
            />
          ))}
        </svg>
      )}
    </div>
  );
}
