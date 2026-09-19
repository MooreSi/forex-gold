import { useCallback, useEffect, useState, type RefObject } from "react";
import type { IChartApi, ISeriesApi } from "lightweight-charts";
import type { Aoi, FibLevel, SetForgetCandidate } from "@/api/types";

/** Where the position box starts, as a fraction of the plot width. */
const BOX_START = 0.62;
/** A band thinner than this is invisible, which reads as a missing zone. */
const MIN_BAND_HEIGHT = 2;

export interface Band {
  key: string;
  kind: Aoi["kind"];
  top: number;
  height: number;
}

export interface FibLine {
  ratio: number;
  price: number;
  y: number;
}

export interface Geometry {
  /** False when a level is off the visible range, so the box cannot be drawn. */
  placeable: boolean;
  entryY: number;
  stopY: number;
  targetY: number;
  left: number;
  right: number;
  bands: Band[];
  fibs: FibLine[];
}

interface Inputs {
  holder: RefObject<HTMLDivElement | null>;
  chart: RefObject<IChartApi | null>;
  series: RefObject<ISeriesApi<"Candlestick"> | null>;
  zones: Aoi[];
  fibLevels: FibLevel[];
  candidate: SetForgetCandidate | null;
  /** Bumped by the caller whenever new data has been pushed into the chart. */
  revision: number;
}

/**
 * Pixel positions for everything drawn OVER the candles.
 *
 * lightweight-charts owns its canvas and has no notion of a price band, a
 * retracement level or TradingView's position tool, so all three are HTML
 * positioned through the chart's own `priceToCoordinate`. That is the point:
 * the overlays and the candles read one price scale, so a band cannot end up a
 * few pixels off the level it claims to be at.
 *
 * Recomputed on every pan, zoom and resize. An overlay positioned once at
 * mount slides off its own levels the first time anyone scrolls the chart —
 * and still looks entirely plausible wherever it lands, which is what makes
 * that failure expensive.
 */
export function useChartGeometry(inputs: Inputs): Geometry | null {
  const { holder, chart, series, zones, fibLevels, candidate, revision } = inputs;
  const [geometry, setGeometry] = useState<Geometry | null>(null);

  const recompute = useCallback(() => {
    const c = chart.current;
    const s = series.current;
    const node = holder.current;
    if (!c || !s || !node) return setGeometry(null);

    const y = (price: number) => s.priceToCoordinate(price);
    // The price scale sits inside the canvas, so the box stops short of it
    // rather than sliding underneath the axis labels.
    const plotWidth = node.clientWidth - c.priceScale("right").width();

    const bands = zones
      .map((z) => {
        const top = y(z.high);
        const bottom = y(z.low);
        if (top === null || bottom === null) return null;
        return {
          key: `${z.kind}-${z.low}-${z.high}`,
          kind: z.kind,
          top: Math.min(top, bottom),
          height: Math.max(Math.abs(bottom - top), MIN_BAND_HEIGHT),
        };
      })
      .filter((b): b is Band => b !== null);

    const fibs = fibLevels
      .map((f) => {
        const at = y(f.price);
        // `priceToCoordinate` returns a branded Coordinate; everything
        // downstream is a CSS pixel, so it is widened once here rather than
        // cast at each use.
        return at === null
          ? null
          : { ratio: f.ratio, price: f.price, y: at as number };
      })
      .filter((f): f is FibLine => f !== null);

    if (!candidate) {
      return setGeometry({
        placeable: false, entryY: 0, stopY: 0, targetY: 0,
        left: plotWidth * BOX_START, right: plotWidth - 2, bands, fibs,
      });
    }

    const entryY = y(candidate.entry);
    const stopY = y(candidate.stop_loss);
    const targetY = y(candidate.take_profit);
    // A level off the top or bottom of the visible range has no coordinate.
    // The BOX is dropped; the bands and the retracement are not. Dropping
    // everything would blank the zones too, and those are the part that is
    // still true when the box cannot be drawn — which is exactly when someone
    // has scrolled away from a resting entry to look at something else.
    const placeable = entryY !== null && stopY !== null && targetY !== null;

    setGeometry({
      entryY: entryY ?? 0, stopY: stopY ?? 0, targetY: targetY ?? 0,
      placeable,
      left: plotWidth * BOX_START,
      right: plotWidth - 2,
      bands,
      fibs,
    });
  }, [chart, series, holder, zones, fibLevels, candidate]);

  useEffect(() => {
    const c = chart.current;
    const node = holder.current;
    if (!c || !node) return;
    const scale = c.timeScale();
    scale.subscribeVisibleTimeRangeChange(recompute);

    // Guarded, and not for the tests. Constructing a ResizeObserver where
    // there is none throws inside a passive effect, and React responds by
    // unmounting the tree: the chart does not degrade, the whole Set & Forget
    // tab goes blank. `window.resize` is the fallback — it misses a container
    // that changes size without the window doing so, which costs a stale
    // overlay until the next pan, and that is a far cheaper failure.
    let stop = () => {};
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(recompute);
      observer.observe(node);
      stop = () => observer.disconnect();
    } else if (typeof window !== "undefined") {
      window.addEventListener("resize", recompute);
      stop = () => window.removeEventListener("resize", recompute);
    }

    recompute();
    return () => {
      scale.unsubscribeVisibleTimeRangeChange(recompute);
      stop();
    };
    // `revision` is in the list so new candle data recomputes too: the scale
    // moves when the series changes, not only when a person touches it.
  }, [recompute, revision, chart, holder]);

  return geometry;
}
