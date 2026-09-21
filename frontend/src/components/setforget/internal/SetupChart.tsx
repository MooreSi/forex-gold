import { useEffect, useRef, useState } from "react";
import {
  ColorType, createChart, CrosshairMode,
  type AutoscaleInfo, type IChartApi, type ISeriesApi, type UTCTimestamp,
} from "lightweight-charts";
import type {
  Aoi, Candle, FibLevel, Overlays, SetForgetCandidate,
} from "@/api/types";
import { chartColours, watchTheme } from "@/components/shared/chartTheme";
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

/**
 * EMA 50 on the accent, EMA 200 on the remote blue — the fast/slow pair Alex
 * G's checklist reads, named as TOKENS rather than hex.
 *
 * Every colour on this chart is read from the theme for the same reason: a
 * canvas cannot use a CSS variable, so it has to be told, and this chart named
 * its own hex until 2026-09-21 — a black rectangle inside a white panel, three
 * clicks from a Chart tab that themed correctly.
 */
function colours() {
  const c = chartColours();
  return { ...c, emas: { "50": c.accent, "200": c.remote } as Record<string, string> };
}

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
/**
 * How many bars the chart opens on: ten days at 4H, enough to read the swing
 * the setup is built on without burying today in fifty days of history.
 */
export const VISIBLE_BARS = 60;

export function SetupChart(props: SetupChartProps) {
  const {
    candles, overlays, zones, fibLevels, candidate, riskMoney, rewardMoney,
  } = props;
  const holder = useRef<HTMLDivElement>(null);
  // The opening range is applied once, on the first candles to arrive.
  const rangeSet = useRef(false);
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
  const [themeTick, setThemeTick] = useState(0);

  // The document attribute rather than `useTheme()`: a chart that throws
  // because a context is missing is a blank panel over a colour, and the
  // colour is the least important thing on it. Same call the Chart tab makes.
  useEffect(() => watchTheme(() => setThemeTick((n) => n + 1)), []);

  const geometry = useChartGeometry({
    holder, chart, series, zones, fibLevels, candidate, revision,
  });

  useEffect(() => {
    if (!holder.current) return;
    const theme = colours();
    const c = createChart(holder.current, {
      layout: {
        background: { type: ColorType.Solid, color: theme.background },
        textColor: theme.text,
        fontFamily: "ui-monospace, SF Mono, Menlo, monospace",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: theme.grid },
        horzLines: { color: theme.grid },
      },
      rightPriceScale: {
        borderColor: theme.border, scaleMargins: { top: 0.12, bottom: 0.12 },
      },
      timeScale: {
        borderColor: theme.border, timeVisible: true, secondsVisible: false,
      },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: true,
    });
    chart.current = c;
    series.current = c.addCandlestickSeries({
      upColor: theme.profit, downColor: theme.loss, borderVisible: false,
      wickUpColor: theme.profit, wickDownColor: theme.loss,
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

  // Repaint on a theme change. Without this the chart holds whichever theme
  // was in force at mount, so switching to light leaves a black rectangle
  // until the tab is navigated away from and back.
  useEffect(() => {
    const c = chart.current;
    if (!c || typeof c.applyOptions !== "function") return;
    const theme = colours();
    c.applyOptions({
      layout: {
        background: { type: ColorType.Solid, color: theme.background },
        textColor: theme.text,
      },
      grid: {
        vertLines: { color: theme.grid },
        horzLines: { color: theme.grid },
      },
      rightPriceScale: { borderColor: theme.border },
      timeScale: { borderColor: theme.border },
    });
    series.current?.applyOptions({
      upColor: theme.profit, downColor: theme.loss,
      wickUpColor: theme.profit, wickDownColor: theme.loss,
    });
    for (const [period, line] of emas.current) {
      line.applyOptions({ color: theme.emas[period] ?? theme.text });
    }
  }, [themeTick]);

  useEffect(() => {
    if (!series.current || candles.length === 0) return;
    series.current.setData(
      candles.map((c) => ({
        time: c.ts as UTCTimestamp,
        open: c.open, high: c.high, low: c.low, close: c.close,
      })),
    );
    // Open on the current market. The panel asks for 300 4H bars because EMA
    // 200 needs them -- fifty days, which drawn all at once leaves today a few
    // pixels wide and makes a live chart look frozen (reported 2026-09-21).
    //
    // ONCE. This panel re-polls every 60 seconds, and re-applying the range on
    // each tick would drag the chart back from wherever the operator had
    // panned it, which is the same class of bug as a poll overwriting a field
    // being typed into.
    if (!rangeSet.current && chart.current) {
      rangeSet.current = true;
      const last = candles.length - 1;
      chart.current.timeScale().setVisibleLogicalRange({
        from: Math.max(0, last - VISIBLE_BARS),
        to: last,
      });
    }
    setRevision((n) => n + 1);
  }, [candles]);

  useEffect(() => {
    if (!chart.current || !overlays) return;
    for (const [period, values] of Object.entries(overlays.emas)) {
      let line = emas.current.get(period);
      if (!line) {
        line = chart.current.addLineSeries({
          color: colours().emas[period] ?? colours().text,
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
                    bg-surface-1 sm:h-96">
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
