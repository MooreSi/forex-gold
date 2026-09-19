/**
 * The Chart tab's wiring.
 *
 * The canvas itself is lightweight-charts and is not re-tested here; what is
 * tested is what the tab ASKS FOR, because that is where it was silently
 * broken: `usePoll` held its entry in a ref, so changing the timeframe
 * re-registered the old entry under the new key and no fetch happened. The
 * chart went on showing 5m candles with the 1H button lit and nothing in the
 * console.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChartPanel } from "../ChartPanel";
import { resetPolls } from "@/hooks/usePoll";

const CANDLES = [
  { ts: 1_750_000_000, open: 2430, high: 2432, low: 2429, close: 2431 },
];

let fetchMock: ReturnType<typeof vi.fn>;
let overlaysBody: Record<string, unknown>;

beforeEach(() => {
  resetPolls();
  overlaysBody = { timeframe: "5m", count: 1, emas: {}, rsi: [], fvgs: [] };
  fetchMock = vi.fn(async (url: string) => {
    if (url.startsWith("/api/chart/candles")) {
      return { ok: true, status: 200, json: async () => CANDLES };
    }
    if (url.startsWith("/api/chart/overlays")) {
      return {
        ok: true, status: 200,
        json: async () => overlaysBody,
      };
    }
    if (url.startsWith("/api/chart/tick")) {
      return {
        ok: true, status: 200,
        json: async () => ({
          bid: 2431.12, ask: 2431.42, mid: 2431.27, spread: 0.3,
          spread_points: 30, timestamp: 1_750_000_000, source: "mt5",
        }),
      };
    }
    return { ok: true, status: 200, json: async () => [] };
  });
  vi.stubGlobal("fetch", fetchMock);
  // lightweight-charts wants a real canvas; jsdom has none.
  //
  // `timeScale` and `priceToCoordinate` were added on 2026-09-19 with the
  // fair-value-gap overlay: the zones are an SVG layer positioned through the
  // chart's own coordinate conversions, so a stand-in that cannot answer
  // "where is this price" is not a stand-in for this component any more.
  // Their geometry is tested for real in fvgGeometry.test.ts.
  vi.mock("lightweight-charts", () => ({
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    createChart: () => ({
      addCandlestickSeries: () => ({
        setData: () => {}, setMarkers: () => {},
        createPriceLine: () => ({}), removePriceLine: () => {},
        priceToCoordinate: (p: number) => p,
      }),
      addLineSeries: () => ({ setData: () => {} }),
      timeScale: () => ({
        getVisibleRange: () => ({ from: 0, to: 2_000_000_000 }),
        // A pixel inside the canvas, as the real one returns. Echoing the
        // timestamp back would put every zone 1.7 billion pixels to the
        // right, which is not a thing the real chart does.
        timeToCoordinate: () => 120,
        subscribeVisibleTimeRangeChange: () => {},
        unsubscribeVisibleTimeRangeChange: () => {},
      }),
      remove: () => {},
    }),
  }));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const asked = (prefix: string) =>
  fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.startsWith(prefix));

describe("what the chart asks for", () => {
  it("opens on 5m", async () => {
    render(<ChartPanel />);

    await waitFor(() => expect(asked("/api/chart/candles").length).toBeGreaterThan(0));
    expect(asked("/api/chart/candles")[0]).toContain("timeframe=5m");
  });

  it("fetches the new timeframe when one is chosen", async () => {
    render(<ChartPanel />);
    await waitFor(() => expect(asked("/api/chart/candles").length).toBeGreaterThan(0));

    await userEvent.click(screen.getByRole("button", { name: "1H" }));

    await waitFor(() => {
      expect(asked("/api/chart/candles").some((u) => u.includes("timeframe=1H"))).toBe(true);
    });
  });

  it("fetches the overlays for the same window as the candles", async () => {
    // Two endpoints that can disagree about the window render an EMA floating
    // off the price.
    render(<ChartPanel />);
    await waitFor(() => expect(asked("/api/chart/overlays").length).toBeGreaterThan(0));

    await userEvent.click(screen.getByRole("button", { name: "1H" }));

    await waitFor(() => {
      expect(asked("/api/chart/overlays").some((u) => u.includes("timeframe=1H"))).toBe(true);
    });
  });

  it("asks for a different bar count when one is chosen", async () => {
    render(<ChartPanel />);
    await waitFor(() => expect(asked("/api/chart/candles").length).toBeGreaterThan(0));

    await userEvent.selectOptions(screen.getByLabelText("Candles shown"), "500");

    await waitFor(() => {
      expect(asked("/api/chart/candles").some((u) => u.includes("count=500"))).toBe(true);
    });
  });
});

describe("when there is nothing to draw", () => {
  it("says it is waiting rather than showing an empty frame", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/chart/tick")) {
        return { ok: true, status: 200, json: async () => null };
      }
      return { ok: true, status: 200, json: async () => [] };
    });
    render(<ChartPanel />);

    expect(await screen.findByText("Waiting for candles")).toBeInTheDocument();
  });

  it("names the bridge when the fetch failed", async () => {
    fetchMock.mockResolvedValue({
      ok: false, status: 500, statusText: "",
      json: async () => ({ error: { kind: "internal", message: "boom", ref: "a1" } }),
    });
    render(<ChartPanel />);

    expect(await screen.findByText("Could not load candles")).toBeInTheDocument();
  });
});

describe("fair-value gaps", () => {
  // jsdom reports every element as 0x0, and a zero-width chart has nowhere to
  // draw. The geometry itself refuses to draw before the chart has a size,
  // which is correct and makes the element size a precondition of this test
  // rather than an implementation detail of it.
  function withSize(width: number, height: number) {
    const w = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
    const h = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true, get: () => width,
    });
    Object.defineProperty(HTMLElement.prototype, "clientHeight", {
      configurable: true, get: () => height,
    });
    return () => {
      if (w) Object.defineProperty(HTMLElement.prototype, "clientWidth", w);
      if (h) Object.defineProperty(HTMLElement.prototype, "clientHeight", h);
    };
  }

  it("draws a zone the backend reported", async () => {
    // The zones have been in the /overlays payload since it was written and
    // nothing drew them. The owner asked for them on 2026-09-19.
    const restore = withSize(600, 400);
    overlaysBody = {
      timeframe: "5m", count: 1, emas: {}, rsi: [],
      fvgs: [{ ts: 1_750_000_000, top: 300, bottom: 250, direction: "bullish" }],
    };
    render(<ChartPanel />);

    expect(await screen.findByTestId("fvg-overlay")).toBeInTheDocument();
    restore();
  });

  it("colours a bearish zone differently from a bullish one", async () => {
    const restore = withSize(600, 400);
    overlaysBody = {
      timeframe: "5m", count: 1, emas: {}, rsi: [],
      fvgs: [{ ts: 1_750_000_000, top: 300, bottom: 250, direction: "bearish" }],
    };
    render(<ChartPanel />);

    const rect = await screen.findByTestId("fvg-bearish-1750000000");
    expect(rect.getAttribute("fill")).toContain("255,68,68");
    restore();
  });

  it("draws no overlay at all when there are no zones", async () => {
    // An empty SVG over the canvas is an invisible element that still
    // intercepts nothing but exists to be wondered about.
    const restore = withSize(600, 400);
    render(<ChartPanel />);
    await screen.findByTestId("candle-chart");

    expect(screen.queryByTestId("fvg-overlay")).not.toBeInTheDocument();
    restore();
  });
});
