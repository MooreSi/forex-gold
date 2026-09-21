/**
 * A stand-in for lightweight-charts.
 *
 * The real library wants a canvas and `window.matchMedia`; jsdom has neither.
 * Shared because there are two charts now — the Chart tab and the ORB report —
 * and a second hand-written stub is how one of them silently stops exercising
 * a method the component relies on.
 *
 * It answers the coordinate conversions with plausible numbers rather than
 * echoing their inputs: a `timeToCoordinate` that returns the timestamp puts
 * every overlay 1.7 billion pixels to the right, which is not a thing the real
 * chart does and would make an overlay test pass for the wrong reason.
 *
 * **It also models DISPOSAL**, because the real library does. After `remove()`
 * every method on the chart and on its time scale throws "Object is disposed",
 * and a stub that stayed friendly hid a real uncaught error: React runs effect
 * cleanups in definition order, so the effect that creates the chart disposed
 * it before the effect that subscribes to its time scale unsubscribed, and
 * every unmount threw. Seen in the browser console on 2026-09-20.
 */
/** Every logical range any stubbed chart was asked to show. */
export const visibleRanges: unknown[] = [];

export function chartStub() {
  return {
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    createChart: () => {
      let disposed = false;
      const alive = <T>(fn: () => T) => (): T => {
        if (disposed) throw new Error("Object is disposed");
        return fn();
      };
      return {
      addCandlestickSeries: () => ({
        setData: () => {},
        applyOptions: () => {},
        setMarkers: () => {},
        createPriceLine: () => ({}),
        removePriceLine: () => {},
        priceToCoordinate: (price: number) => price,
      }),
      addLineSeries: () => ({ setData: () => {} }),
      applyOptions: () => {},
      priceScale: () => ({ applyOptions: () => {}, width: () => 60 }),
      timeScale: alive(() => ({
        getVisibleRange: () => ({ from: 0, to: 2_000_000_000 }),
        timeToCoordinate: () => 120,
        subscribeVisibleTimeRangeChange: alive(() => {}),
        unsubscribeVisibleTimeRangeChange: alive(() => {}),
        // Recorded so a test can assert which bars a chart opens on.
        setVisibleLogicalRange: (r: unknown) => { visibleRanges.push(r); },
      })),
      remove: () => { disposed = true; },
      };
    },
  };
}
