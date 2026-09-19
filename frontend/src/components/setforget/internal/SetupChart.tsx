import { useEffect, useRef, useState } from "react";
import {
  ColorType, createChart, CrosshairMode,
  type AutoscaleInfo, type IChartApi, type ISeriesApi, type UTCTimestamp,
} from "lightweight-charts";
import type {
  Aoi, Candle, FibLevel, Overlays, SetForgetCandidate,
} from "@/api/types";
import { useChartGeometry } from "../hooks/useChartGeometry";
import { FibonacciOverlay } from "./FibonacciOverlay";
import { PositionOverlay } from "./PositionOverlay";

interface SetupChartProps {
  candles: Candle[];
  overlays: Overlays | null;
  zones: Aoi[];
  fibLevels: FibLevel[];
  candidate: SetForgetCandidate | null;
  riskMoney: number | null;
  rewardMoney: number | null;
}

// The same green and red the rest of the app uses for rising and falling. Not
// chosen freely: they carry the profit/loss meaning everywhere else on screen.
const BULL = "#00cc88";
const BEAR = "#ff4444";
// EMA 50 gold, EMA 200 sky blue — the fast/slow pair Alex G's checklist reads.
const EMA_COLOURS: Record<string, string> = { "50": "#ffd700", "200": "#64b4ff" };

/**
 * The section's chart: candles, the two EMAs, the areas of interest, the
 * Fibonacci retracement and the long/short position box.
 *
 * This file owns the chart's LIFECYCLE and nothing else — creating it, pushing
 * data in, and composing the overlays. Where the overlays land is
 * `useChartGeometry`; what they look like is each overlay's own file. The
 * split is not tidiness: the two bugs this chart has already had were both in
 * the geometry, and geometry that lives inside a component that also owns a
 * canvas cannot be tested without one.
 */
export function SetupChart(props: SetupChartProps) {
  const {
    candles, overlays, zones, fibLevels, candidate, riskMoney, rewardMoney,
  } = props;
  const holder = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const emas = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  // lightweight-charts calls the autoscale provider on its own schedule, so it
  // reads the candidate through a ref rather than closing over a stale one.
  const latest = useRef<SetForgetCandidate | null>(candidate);
  latest.current = candidate;
  // Bumped when new data has been pushed in, so the geometry recomputes: the
  // price scale moves when the series changes, not only when a person pans.
  const [revision, setRevision] = useState(0);

  const geometry = useChartGeometry({
    holder, chart, series, zones, fibLevels, candidate, revision,
  });

  useEffect(() => {
    if (!holder.current) return;
    const c = createChart(holder.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#030712" },
        textColor: "#9ca3af",
        fontFamily: "ui-monospace, SF Mono, Menlo, monospace",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "#131a28" },
        horzLines: { color: "#131a28" },
      },
      rightPriceScale: { borderColor: "#263044", scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: "#263044", timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: true,
    });
    chart.current = c;
    series.current = c.addCandlestickSeries({
      upColor: BULL, downColor: BEAR, borderVisible: false,
      wickUpColor: BULL, wickDownColor: BEAR,
      priceLineVisible: false,
    });
    // Without this the scale fits the CANDLES, and a resting entry a few
    // hundred points below them falls off the bottom of the chart — the
    // position box then has no coordinates and silently does not draw, on a
    // page whose whole point is that box.
    series.current.applyOptions({
      autoscaleInfoProvider: (original: () => AutoscaleInfo | null) => {
        const info = original();
        const setup = latest.current;
        if (!info || !setup) return info;
        const levels = [setup.entry, setup.stop_loss, setup.take_profit];
        return {
          ...info,
          priceRange: {
            minValue: Math.min(info.priceRange.minValue, ...levels),
            maxValue: Math.max(info.priceRange.maxValue, ...levels),
          },
        };
      },
    });
    setRevision((n) => n + 1);
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
      emas.current.clear();
    };
  }, []);

  useEffect(() => {
    if (!series.current || candles.length === 0) return;
    series.current.setData(
      candles.map((c) => ({
        time: c.ts as UTCTimestamp,
        open: c.open, high: c.high, low: c.low, close: c.close,
      })),
    );
    setRevision((n) => n + 1);
  }, [candles]);

  useEffect(() => {
    if (!chart.current || !overlays) return;
    for (const [period, values] of Object.entries(overlays.emas)) {
      let line = emas.current.get(period);
      if (!line) {
        line = chart.current.addLineSeries({
          color: EMA_COLOURS[period] ?? "#9ca3af",
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          title: `EMA ${period}`,
        });
        emas.current.set(period, line);
      }
      line.setData(
        values
          .map((v, i) => ({ time: candles[i]?.ts as UTCTimestamp, value: v }))
          .filter((p): p is { time: UTCTimestamp; value: number } =>
            p.time !== undefined && p.value !== null && Number.isFinite(p.value)),
      );
    }
  }, [overlays, candles]);

  return (
    <div className="relative h-72 w-full overflow-hidden rounded border border-line
                    bg-surface-0 sm:h-96">
      <div ref={holder} data-testid="setforget-chart" className="h-full w-full" />

      {geometry && <FibonacciOverlay fibs={geometry.fibs} />}

      {geometry?.bands.map((b) => (
        <div
          key={b.key}
          aria-hidden
          // z-10: over the retracement and the candles, under the position
          // box. See the note in PositionOverlay — the chart's own panes carry
          // a z-index of their own, and an overlay without one loses.
          className={`pointer-events-none absolute left-0 right-0 z-10 ${
            b.kind === "demand"
              ? "bg-profit/[0.13] border-y border-profit/40"
              : "bg-loss/[0.13] border-y border-loss/40"
          }`}
          style={{ top: b.top, height: b.height }}
        />
      ))}

      {candidate && geometry?.placeable && (
        <PositionOverlay
          direction={candidate.direction}
          entry={candidate.entry}
          stopLoss={candidate.stop_loss}
          takeProfit={candidate.take_profit}
          entryY={geometry.entryY}
          stopY={geometry.stopY}
          targetY={geometry.targetY}
          left={geometry.left}
          right={geometry.right}
          riskMoney={riskMoney}
          rewardMoney={rewardMoney}
          rr={candidate.rr}
        />
      )}
    </div>
  );
}
