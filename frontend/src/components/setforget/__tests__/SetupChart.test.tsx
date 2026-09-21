/**
 * The chart's own logic: the price scale it asks for, and where the overlay
 * lands on it.
 *
 * This file exists because of two bugs that no test caught and only looking at
 * the running page did.
 *
 * **The scale fitted the candles.** A resting entry a few hundred points below
 * them fell off the bottom, `priceToCoordinate` returned null for every level,
 * and the position box silently did not draw — on a page whose entire point is
 * that box. Fixed with an autoscale provider; the provider is called directly
 * below rather than trusted.
 *
 * **The box painted underneath the candles.** lightweight-charts stacks its own
 * panes, so an overlay with no z-index of its own is laid out perfectly and
 * then covered. There is no way to assert paint order in jsdom, which has no
 * layout — so what is asserted is the stacking class, and the comment is the
 * reason it is worth asserting at all.
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SetupChart } from "../internal/SetupChart";
import type { Aoi, FibLevel, SetForgetCandidate } from "@/api/types";

/** Whatever the component last handed to the SERIES' `applyOptions`. */
let options: Record<string, unknown>;
/** The chart's creation options, its later applyOptions calls, and the
 *  candlestick series' own options -- all three carry theme colours. */
type Options = Record<string, any>;
let created: Options;
let applied: Options[];
let seriesOptions: Options;
/** The prices the fake scale can place. Anything else answers null. */
let inRange: (price: number) => boolean;

vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CrosshairMode: { Normal: 0 },
  createChart: (_node: unknown, opts: Options) => ((created = opts), {
    applyOptions: (next: Options) => { applied.push(next); },
    addCandlestickSeries: (o: Options) => ((seriesOptions = o), {
      setData: () => {},
      // Merges, like the real one. A mock that REPLACED made the theme
      // repaint look as though it had wiped the autoscale provider.
      applyOptions: (next: Record<string, unknown>) => {
        options = { ...options, ...next };
      },
      // A linear scale: 2000 sits at y=200, and every point is 2px.
      priceToCoordinate: (price: number) =>
        (inRange(price) ? 200 - (price - 2000) * 2 : null),
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
  { ts: 1_750_014_400, open: 2002, high: 2008, low: 1999, close: 2006 },
];

const CANDIDATE: SetForgetCandidate = {
  direction: "BUY", entry: 1985, stop_loss: 1972, take_profit: 2040,
  order_type: "limit", risk: 13, reward: 55, rr: 4.23,
  stage: "triggered",
  trigger: { kind: "shift_of_structure", ts: 1, level: 1972 },
  distance: 15, distance_days: 0.75,
};

const FIBS: FibLevel[] = [
  { ratio: 0.382, price: 1990 },
  { ratio: 0.786, price: 1978 },
];

const ZONES: Aoi[] = [
  { kind: "demand", low: 1975, high: 1985, ts: 1, touches: 2 },
  { kind: "supply", low: 2040, high: 2050, ts: 2, touches: 1 },
];

function draw(over: Partial<Parameters<typeof SetupChart>[0]> = {}) {
  return render(
    <SetupChart
      candles={CANDLES}
      overlays={null}
      zones={ZONES}
      fibLevels={FIBS}
      candidate={CANDIDATE}
      riskMoney={260}
      rewardMoney={1100}
      {...over}
    />,
  );
}

beforeEach(() => {
  options = {};
  created = {};
  applied = [];
  seriesOptions = {};
  inRange = () => true;
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => vi.unstubAllGlobals());

describe("the price scale it asks for", () => {
  it("widens the range to cover the setup's own levels", () => {
    draw();
    const provider = options.autoscaleInfoProvider as
      (o: () => unknown) => { priceRange: { minValue: number; maxValue: number } };

    // The candles alone would give 1995–2008; the setup runs 1972–2040.
    const widened = provider(() => ({ priceRange: { minValue: 1995, maxValue: 2008 } }));

    expect(widened.priceRange.minValue).toBe(1972);
    expect(widened.priceRange.maxValue).toBe(2040);
  });

  it("leaves a range that already covers the setup alone", () => {
    draw();
    const provider = options.autoscaleInfoProvider as
      (o: () => unknown) => { priceRange: { minValue: number; maxValue: number } };

    const same = provider(() => ({ priceRange: { minValue: 1900, maxValue: 2100 } }));

    expect(same.priceRange).toEqual({ minValue: 1900, maxValue: 2100 });
  });

  it("passes a null range straight through rather than inventing one", () => {
    draw();
    const provider = options.autoscaleInfoProvider as (o: () => unknown) => unknown;

    expect(provider(() => null)).toBeNull();
  });

  it("does not widen anything when there is no setup", () => {
    draw({ candidate: null });
    const provider = options.autoscaleInfoProvider as
      (o: () => unknown) => { priceRange: { minValue: number } };

    const untouched = provider(() => ({ priceRange: { minValue: 1995, maxValue: 2008 } }));

    expect(untouched.priceRange.minValue).toBe(1995);
  });
});

describe("what gets drawn over the candles", () => {
  it("places each zone band at its own two prices", () => {
    const { container } = draw();
    const bands = [...container.querySelectorAll("div[aria-hidden]")]
      .filter((d) => d.className.includes("left-0"));

    // demand 1975–1985 → y 250 down to 230, so top 230 and height 20.
    const demand = bands.find((b) => b.className.includes("bg-profit")) as HTMLElement;
    expect(demand.style.top).toBe("230px");
    expect(demand.style.height).toBe("20px");
  });

  it("gives a band with no height on screen a visible minimum", () => {
    const { container } = draw({
      zones: [{ kind: "demand", low: 2000, high: 2000, ts: 1, touches: 1 }],
    });
    const band = [...container.querySelectorAll("div[aria-hidden]")]
      .find((d) => d.className.includes("left-0")) as HTMLElement;

    // A hairline nobody can see is indistinguishable from a missing zone.
    expect(parseFloat(band.style.height)).toBeGreaterThan(0);
  });

  it("stacks the overlay and the bands above the chart's own panes", () => {
    /* jsdom has no layout, so paint order cannot be observed — only the
       stacking classes can. They are asserted anyway: their absence is the
       exact regression that rendered a perfect box underneath the candles. */
    const { container } = draw();
    const band = [...container.querySelectorAll("div[aria-hidden]")]
      .find((d) => d.className.includes("left-0")) as HTMLElement;

    expect(band.className).toMatch(/\bz-10\b/);
    expect(screen.getByTestId("position-overlay").className).toMatch(/\bz-20\b/);
  });
});

describe("when a level is off the visible range", () => {
  it("drops the box rather than drawing it at the wrong price", () => {
    inRange = (price) => price > 1990;          // the stop at 1972 has no y

    draw();

    expect(screen.queryByTestId("position-overlay")).not.toBeInTheDocument();
  });

  it("keeps the zone bands that can still be placed", () => {
    /* The zones are the part that is still true when the box cannot be drawn,
       which is exactly when someone has scrolled away from a resting entry. */
    inRange = (price) => price > 1990;
    const { container } = draw();

    const bands = [...container.querySelectorAll("div[aria-hidden]")]
      .filter((d) => d.className.includes("left-0"));

    expect(bands).toHaveLength(1);              // the supply band at 2040–2050
    expect(bands[0].className).toContain("bg-loss");
  });

  it("draws no box at all when there is no setup", () => {
    draw({ candidate: null });

    expect(screen.queryByTestId("position-overlay")).not.toBeInTheDocument();
  });
});

describe("the retracement levels", () => {
  it("places each one at its own price on the chart's scale", () => {
    draw();

    // 1990 → y 220 and 1978 → y 244 on the fake scale above.
    expect(screen.getByText("38.2% · 1990.00").parentElement?.style.top)
      .toBe("220px");
    expect(screen.getByText("78.6% · 1978.00").parentElement?.style.top)
      .toBe("244px");
  });

  it("drops a level that is off the visible range and keeps the rest", () => {
    /* Dropping the whole band because one edge scrolled away would remove the
       context at exactly the moment someone is looking at the other edge. */
    inRange = (price) => price > 1985;          // 1978 has no coordinate

    draw();

    expect(screen.getByText("38.2% · 1990.00")).toBeInTheDocument();
    expect(screen.queryByText(/78\.6%/)).not.toBeInTheDocument();
    // Dropped, not substituted. A level kept as a zero would render as
    // "0.0% · 0.00" at the top of the chart -- a line that looks like a real
    // reading and is not one.
    expect(screen.getAllByText(/% · /)).toHaveLength(1);
    expect(screen.queryByText(/0\.0% · 0\.00/)).not.toBeInTheDocument();
  });

  it("draws no retracement at all when no leg has completed", () => {
    draw({ fibLevels: [] });

    expect(screen.queryByTestId("fibonacci-overlay")).not.toBeInTheDocument();
  });

  it("still draws the retracement when there is no setup to box", () => {
    /* The band is a reading of the chart, not of the trade. It is the most
       useful thing on screen on a day when the rules refuse everything. */
    draw({ candidate: null });

    expect(screen.getByTestId("fibonacci-overlay")).toBeInTheDocument();
    expect(screen.queryByTestId("position-overlay")).not.toBeInTheDocument();
  });
});

describe("the theme", () => {
  /** The options the chart was created with, and any applied since. */
  function chartOptions(): Options {
    return applied.length > 0 ? applied[applied.length - 1] : created;
  }

  it("takes its colours from the theme rather than hard-coding them", () => {
    /* Reported 2026-09-21: this chart was black inside a white panel, because
       it named #030712 directly while the Chart tab read the tokens. A canvas
       cannot use a CSS variable, so the colours have to be READ and handed
       over -- and read from the one shared place, or the third chart in the
       app drifts from the other two exactly as this one did. */
    document.documentElement.style.setProperty("--color-surface-1", "#ffffff");
    document.documentElement.style.setProperty("--color-ink-2", "#111111");

    draw();

    expect(created.layout.background.color).toBe("#ffffff");
    expect(created.layout.textColor).toBe("#111111");
  });

  it("repaints when the theme changes rather than keeping the one it was born with",
     async () => {
    /* The chart is created once. Without this it holds whichever theme was in
       force at mount, so switching to light leaves a black rectangle until the
       tab is navigated away from and back. */
    document.documentElement.style.setProperty("--color-surface-1", "#000000");
    draw();
    expect(chartOptions().layout.background.color).toBe("#000000");

    document.documentElement.style.setProperty("--color-surface-1", "#ffffff");
    // A MutationObserver delivers on a microtask, so this has to be awaited
    // or the assertion runs before the repaint.
    await act(async () => {
      document.documentElement.setAttribute("data-theme", "light");
    });

    expect(chartOptions().layout.background.color).toBe("#ffffff");
  });

  it("draws the candles in the app's own profit and loss colours", () => {
    document.documentElement.style.setProperty("--color-profit", "#00aa77");
    document.documentElement.style.setProperty("--color-loss", "#dd2222");

    draw();

    expect(seriesOptions.upColor).toBe("#00aa77");
    expect(seriesOptions.downColor).toBe("#dd2222");
  });
});
