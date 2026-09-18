import { useEffect, useRef } from "react";
import {
  ColorType, createChart, CrosshairMode,
  type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { Candle, Overlays, Tick, Trade } from "@/api/types";

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

  useEffect(() => {
    if (!holder.current) return;
    const c = createChart(holder.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#030712" },
        textColor: "#9ca3af",
        fontFamily: "ui-monospace, SF Mono, Menlo, monospace",
      },
      grid: {
        vertLines: { color: "#1b2333" },
        horzLines: { color: "#1b2333" },
      },
      rightPriceScale: { borderColor: "#263044" },
      timeScale: { borderColor: "#263044", timeVisible: true, secondsVisible: false },
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

  return <div ref={holder} data-testid="candle-chart" className="h-full w-full" />;
}
