/**
 * The geometry hook's resilience, as opposed to its arithmetic (which
 * `SetupChart.test.tsx` covers through the component).
 *
 * `ResizeObserver` is the whole file. The hook watches the chart's container
 * so the overlays follow a resize, and constructing one unguarded means that
 * anywhere it is missing — jsdom, an older Safari, a server render — the
 * constructor throws inside a passive effect and React unmounts the entire
 * section. The chart does not degrade; the Set & Forget tab goes blank.
 *
 * This was found as a flaky test rather than as a bug report: the panel's own
 * suite stubbed the global in `beforeEach` and cleared it in `afterEach`, and
 * a passive effect that landed after the clear failed about one run in three.
 * The flake was the symptom; the missing guard was the defect.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SetupChart } from "../internal/SetupChart";

vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CrosshairMode: { Normal: 0 },
  createChart: () => ({
    addCandlestickSeries: () => ({
      setData: () => {},
      applyOptions: () => {},
      priceToCoordinate: (price: number) => 200 - (price - 2000) * 2,
    }),
    addLineSeries: () => ({ setData: () => {} }),
    priceScale: () => ({ width: () => 60 }),
    timeScale: () => ({
      // The real chart has this; the panel opens on the recent bars with it.
      setVisibleLogicalRange: () => {},
      subscribeVisibleTimeRangeChange: () => {},
      unsubscribeVisibleTimeRangeChange: () => {},
    }),
    remove: () => {},
  }),
}));

const CANDLES = [
  { ts: 1_750_000_000, open: 2000, high: 2005, low: 1995, close: 2002 },
];

function draw() {
  return render(
    <SetupChart
      candles={CANDLES}
      overlays={null}
      zones={[{ kind: "demand", low: 1975, high: 1985, ts: 1, touches: 2 }]}
      fibLevels={[{ ratio: 0.382, price: 1990 }]}
      candidate={null}
      riskMoney={null}
      rewardMoney={null}
    />,
  );
}

const original = globalThis.ResizeObserver;
afterEach(() => {
  globalThis.ResizeObserver = original;
  vi.unstubAllGlobals();
});

describe("when the browser has no ResizeObserver", () => {
  beforeEach(() => {
    // @ts-expect-error - deliberately removing it, which is the whole point.
    delete globalThis.ResizeObserver;
  });

  it("still renders the chart rather than taking the section down", () => {
    expect(() => draw()).not.toThrow();
    expect(screen.getByTestId("setforget-chart")).toBeInTheDocument();
  });

  it("still places the overlays it can compute", () => {
    draw();

    expect(screen.getByText("38.2% · 1990.00")).toBeInTheDocument();
  });
});

describe("when the browser has one", () => {
  let observed: unknown[];

  beforeEach(() => {
    observed = [];
    vi.stubGlobal("ResizeObserver", class {
      observe(node: unknown) { observed.push(node); }
      unobserve() {}
      disconnect() {}
    });
  });

  it("watches the chart's own container", () => {
    draw();

    expect(observed).toHaveLength(1);
    // The element the chart actually fills, not its sized wrapper -- it is the
    // one whose clientWidth the plot width is measured from.
    expect(observed[0]).toBe(screen.getByTestId("setforget-chart"));
  });
});
