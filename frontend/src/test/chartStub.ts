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
 *
 * **A SERIES is the harder half, and it is why this is not one `throw`.** A
 * disposed *series* does not throw. `removePriceLine` reaches the model,
 * `updateSource` marks the chart dirty and a repaint is queued with
 * `requestAnimationFrame` — and it is that callback, a frame later, that hits
 * the disposed object. The error surfaces as an uncaught "Object is disposed"
 * with no application frame anywhere in its stack, long after the component
 * that caused it has gone. Three of them per unmount, on 2026-09-22, from the
 * Chart tab's bid/ask price lines.
 *
 * So the series records instead of throwing, and `touchedAfterRemove` is the
 * assertion: nothing may touch a chart object after `remove()`. Making it
 * throw here would be a stub inventing a behaviour to catch a real bug, and
 * the next person would spend an afternoon looking for the try/except that
 * ought to exist.
 */
/** Every logical range any stubbed chart was asked to show. */
export const visibleRanges: unknown[] = [];

/**
 * Names of the chart methods called after `remove()`, newest last.
 *
 * Reset it in `beforeEach`; it is module state, shared by every test in a
 * file. Anything in here after an unmount is an uncaught error in the browser.
 */
export const touchedAfterRemove: string[] = [];

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
      // The series' half: no throw, a note. See the file comment.
      const noted = <A extends unknown[], T>(name: string, fn: (...a: A) => T) =>
        (...args: A): T => {
          if (disposed) touchedAfterRemove.push(name);
          return fn(...args);
        };
      return {
      addCandlestickSeries: () => ({
        setData: noted("setData", () => {}),
        applyOptions: noted("applyOptions", () => {}),
        setMarkers: noted("setMarkers", () => {}),
        createPriceLine: noted("createPriceLine", () => ({})),
        removePriceLine: noted("removePriceLine", () => {}),
        priceToCoordinate: noted("priceToCoordinate", (price: number) => price),
      }),
      addLineSeries: () => ({ setData: noted("setData", () => {}) }),
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
