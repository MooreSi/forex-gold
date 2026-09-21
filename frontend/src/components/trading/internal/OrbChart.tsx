import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType, createChart, CrosshairMode,
  type IChartApi, type ISeriesApi, type IPriceLine, type UTCTimestamp,
} from "lightweight-charts";
import { api } from "@/api/client";
import { EmptyState } from "@/components/shared/EmptyState";
import { chartColours, rgba, watchTheme } from "@/components/shared/chartTheme";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";
import type { Candle } from "@/api/types";

interface OrbBands {
  asia_low: number; asia_high: number;
  or_low: number; or_high: number;
  stop: number; target: number; target2: number | null;
  direction: string;
}

/**
 * The ORB report on the same chart the rest of the app uses.
 *
 * It was a server-rendered PNG: a matplotlib image, base64'd into the payload,
 * that could not be zoomed, panned or read against a live price, and that
 * looked nothing like the Chart tab three clicks away. The owner asked for the
 * TradingView chart on 2026-09-19.
 *
 * **The two ranges are the whole report.** The Asian session is the
 * confirmation filter and the first fifteen minutes of London is the traded
 * range, so they are drawn as bands across the full width — they are price
 * zones, not events at a moment, and a band that stopped where the candles
 * stop would read as a level that expired.
 *
 * Stop and target are price lines rather than bands, because they are single
 * prices and the chart's own axis labels then show them.
 *
 * 5-minute candles over a day: the opening range is fifteen minutes, which is
 * three bars, and anything coarser cannot show it at all.
 */
const TIMEFRAME = "5m";
const BARS = 288;

interface Band {
  key: string; label: string; colour: string;
  top: number; bottom: number;
}

export function OrbChart({ bands }: { bands: OrbBands | null }) {
  const holder = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  // See CandleChart for the whole story: lightweight-charts disposes hard, and
  // React runs effect cleanups in definition order, so the chart is already
  // gone by the time the subscription below tries to unsubscribe from it.
  const disposed = useRef(false);
  const [themeTick, setThemeTick] = useState(0);
  const [rects, setRects] = useState<(Band & { y: number; height: number })[]>([]);

  const candles = usePoll<Candle[]>(
    `orb/candles/${TIMEFRAME}`,
    useCallback(() => api.get<Candle[]>(
      `/api/chart/candles?timeframe=${TIMEFRAME}&count=${BARS}`), []),
    30_000,
  );

  useEffect(() => watchTheme(() => setThemeTick((n) => n + 1)), []);

  useEffect(() => {
    if (!holder.current) return;
    const c = chartColours();
    const instance = createChart(holder.current, {
      layout: {
        background: { type: ColorType.Solid, color: c.background },
        textColor: c.text,
        fontFamily: "ui-monospace, SF Mono, Menlo, monospace",
      },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.border },
      timeScale: { borderColor: c.border, timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: true,
    });
    chart.current = instance;
    series.current = instance.addCandlestickSeries({
      upColor: c.profit, downColor: c.loss, borderVisible: false,
      wickUpColor: c.profit, wickDownColor: c.loss,
    });
    disposed.current = false;
    return () => {
      disposed.current = true;
      instance.remove();
      chart.current = null;
      series.current = null;
      priceLines.current = [];
    };
  }, []);

  useEffect(() => {
    const c = chart.current;
    if (!c || typeof c.applyOptions !== "function") return;
    const colours = chartColours();
    c.applyOptions({
      layout: {
        background: { type: ColorType.Solid, color: colours.background },
        textColor: colours.text,
      },
      grid: { vertLines: { color: colours.grid }, horzLines: { color: colours.grid } },
      rightPriceScale: { borderColor: colours.border },
      timeScale: { borderColor: colours.border },
    });
  }, [themeTick]);

  // useMemo, not a bare asArray: this array is an effect dependency below,
  // and a fresh reference on every render turns "redraw when the candles
  // change" into "redraw, setState, render, redraw" -- an infinite loop that
  // hangs the test runner rather than failing it.
  const rows = useMemo(() => asArray<Candle>(candles.data), [candles.data]);

  useEffect(() => {
    if (!series.current) return;
    series.current.setData(rows.map((r) => ({
      time: r.ts as UTCTimestamp,
      open: r.open, high: r.high, low: r.low, close: r.close,
    })));
  }, [rows]);

  // Stop and target as price lines: they are single prices, and the axis then
  // labels them without anything here drawing text.
  useEffect(() => {
    const s = series.current;
    if (!s) return;
    for (const line of priceLines.current) s.removePriceLine(line);
    priceLines.current = [];
    if (!bands) return;
    const c = chartColours();
    const add = (price: number | null, colour: string, title: string) => {
      if (price == null || !Number.isFinite(price)) return;
      priceLines.current.push(s.createPriceLine({
        price, color: colour, lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title,
      }));
    };
    add(bands.stop, c.loss, "Stop");
    add(bands.target, c.profit, "Target");
    add(bands.target2, c.profit, "Target 2");
  }, [bands, themeTick, rows]);

  const redraw = useCallback(() => {
    const s = series.current;
    const box = holder.current;
    // An empty update is skipped rather than set: setting a fresh [] on every
    // pass is the same loop the memo above avoids.
    if (disposed.current || !s || !box || !bands) {
      return setRects((prev) => (prev.length ? [] : prev));
    }
    const c = chartColours();

    const zones: Band[] = [
      { key: "asia", label: "Asian range", colour: c.remote,
        top: Math.max(bands.asia_high, bands.asia_low),
        bottom: Math.min(bands.asia_high, bands.asia_low) },
      { key: "or", label: "Opening range", colour: c.accent,
        top: Math.max(bands.or_high, bands.or_low),
        bottom: Math.min(bands.or_high, bands.or_low) },
    ];

    const out: (Band & { y: number; height: number })[] = [];
    for (const zone of zones) {
      const yTop = s.priceToCoordinate(zone.top);
      const yBottom = s.priceToCoordinate(zone.bottom);
      // A band whose prices are off the visible range has no coordinate, and
      // drawing it at zero would put a level across the top of the chart at a
      // price that is not there.
      if (yTop == null || yBottom == null) continue;
      out.push({
        ...zone,
        y: Math.min(yTop, yBottom),
        // A range of zero is still a range -- the Asian session can be flat.
        height: Math.max(Math.abs(yBottom - yTop), 1),
      });
    }
    setRects((prev) => (prev.length === out.length
      && prev.every((p, i) => p.key === out[i]!.key && p.y === out[i]!.y
        && p.height === out[i]!.height)
      ? prev
      : out));
  }, [bands]);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    redraw();
    const scale = c.timeScale();
    scale.subscribeVisibleTimeRangeChange(redraw);
    return () => {
      if (disposed.current) return;
      scale.unsubscribeVisibleTimeRangeChange(redraw);
    };
  }, [redraw, rows, themeTick]);

  if (!candles.data && candles.error) {
    return <EmptyState title="Could not load the candles" hint={candles.error.message} />;
  }

  return (
    <div className="space-y-1">
      <div ref={holder} data-testid="orb-chart" className="relative h-72 w-full">
        {rects.length > 0 && (
          <svg
            data-testid="orb-bands"
            // z-10: lightweight-charts gives its own canvases z-index 1 and 2,
            // so an overlay at `auto` is painted underneath them.
            className="pointer-events-none absolute inset-0 z-10 h-full w-full"
            aria-hidden
          >
            {rects.map((r) => (
              <rect
                key={r.key}
                data-testid={`orb-band-${r.key}`}
                x="0" y={r.y} width="100%" height={r.height}
                fill={rgba(r.colour, 0.14)}
                stroke={rgba(r.colour, 0.5)}
                strokeWidth="0.5"
              />
            ))}
          </svg>
        )}
      </div>
      <div className="flex flex-wrap gap-3 text-[10px] text-ink-3">
        <span className="flex items-center gap-1">
          <span className="inline-block size-2 rounded-sm bg-remote/40" /> Asian range
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block size-2 rounded-sm bg-accent/40" /> Opening range
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-px w-3 bg-loss" /> Stop line
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-px w-3 bg-profit" /> Target line
        </span>
      </div>
    </div>
  );
}
