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

beforeEach(() => {
  resetPolls();
  fetchMock = vi.fn(async (url: string) => {
    if (url.startsWith("/api/chart/candles")) {
      return { ok: true, status: 200, json: async () => CANDLES };
    }
    if (url.startsWith("/api/chart/overlays")) {
      return {
        ok: true, status: 200,
        json: async () => ({ timeframe: "5m", count: 1, emas: {}, rsi: [], fvgs: [] }),
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
  vi.mock("lightweight-charts", () => ({
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    createChart: () => ({
      addCandlestickSeries: () => ({
        setData: () => {}, setMarkers: () => {},
        createPriceLine: () => ({}), removePriceLine: () => {},
      }),
      addLineSeries: () => ({ setData: () => {} }),
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
