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
 */
export function chartStub() {
  return {
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    createChart: () => ({
      addCandlestickSeries: () => ({
        setData: () => {},
        setMarkers: () => {},
        createPriceLine: () => ({}),
        removePriceLine: () => {},
        priceToCoordinate: (price: number) => price,
      }),
      addLineSeries: () => ({ setData: () => {} }),
      applyOptions: () => {},
      timeScale: () => ({
        getVisibleRange: () => ({ from: 0, to: 2_000_000_000 }),
        timeToCoordinate: () => 120,
        subscribeVisibleTimeRangeChange: () => {},
        unsubscribeVisibleTimeRangeChange: () => {},
      }),
      remove: () => {},
    }),
  };
}
