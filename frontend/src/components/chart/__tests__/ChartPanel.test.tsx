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
import { barUpdates, touchedAfterRemove } from "@/test/chartStub";
import { resetTradingViewHost } from "../internal/TradingViewChart";

// lightweight-charts wants a real canvas and `window.matchMedia`; jsdom has
// neither. The stand-in is shared with the ORB chart's tests -- see
// src/test/chartStub.ts for why it answers coordinates the way it does.
//
// The factory is async with a dynamic import because `vi.mock` is hoisted
// above every import in the file, so a factory that closes over one cannot
// see it yet.
vi.mock("lightweight-charts", async () => (await import("@/test/chartStub")).chartStub());

const CANDLES = [
  { ts: 1_750_000_000, open: 2430, high: 2432, low: 2429, close: 2431 },
];

let fetchMock: ReturnType<typeof vi.fn>;
let overlaysBody: Record<string, unknown>;

beforeEach(() => {
  resetPolls();
  touchedAfterRemove.length = 0;
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

  it("labels each zone FVG so it is not just a coloured band", async () => {
    // The owner could not tell what the red and green bands were (2026-09-20).
    // A zone that is only a colour is a zone you have to be told about.
    const restore = withSize(600, 400);
    overlaysBody = {
      timeframe: "5m", count: 1, emas: {}, rsi: [],
      fvgs: [{ ts: 1_750_000_000, top: 300, bottom: 250, direction: "bullish" }],
    };
    render(<ChartPanel />);

    const label = await screen.findByTestId("fvg-label-bullish-1750000000");
    expect(label.textContent).toBe("FVG");
    restore();
  });

  it("puts the label inside its own zone, not at the chart origin", async () => {
    // A label drawn at 0,0 is present in the DOM and useless on screen — the
    // same failure mode the zones themselves had before the z-index fix.
    const restore = withSize(600, 400);
    overlaysBody = {
      timeframe: "5m", count: 1, emas: {}, rsi: [],
      fvgs: [{ ts: 1_750_000_000, top: 300, bottom: 250, direction: "bearish" }],
    };
    render(<ChartPanel />);

    const rect = await screen.findByTestId("fvg-bearish-1750000000");
    const label = await screen.findByTestId("fvg-label-bearish-1750000000");
    const x = Number(rect.getAttribute("x"));
    const y = Number(rect.getAttribute("y"));
    const h = Number(rect.getAttribute("height"));
    expect(Number(label.getAttribute("x"))).toBeGreaterThan(x);
    expect(Number(label.getAttribute("y"))).toBeCloseTo(y + h / 2, 5);
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

/**
 * Leaving the tab must leave nothing behind.
 *
 * Three uncaught "Object is disposed" errors landed in the browser console
 * every time the Chart tab was left after the window had been resized,
 * 2026-09-22. None of them carried an application frame: the throw happens a
 * frame later, inside lightweight-charts' own `requestAnimationFrame` repaint,
 * so the stack points at the library and the cause is already gone.
 *
 * The cause is ordering. React runs a component's effect cleanups in the order
 * the effects were DEFINED, and the effect that creates the chart is defined
 * first — so `chart.remove()` has already run by the time the bid/ask effect's
 * cleanup removes its two price lines. Removing a price line reaches the
 * model, the model queues a repaint, and the repaint finds a disposed object.
 *
 * `disposed` already existed in this component, with a comment explaining this
 * exact hazard, and guarded the time-scale unsubscribe. The price lines were
 * the half that was missed.
 */
describe("leaving the tab", () => {
  it("touches nothing on the chart after it has been removed", async () => {
    const { unmount } = render(<ChartPanel />);
    await screen.findByTestId("candle-chart");
    // The bid/ask lines only exist once a tick has arrived, and it is their
    // cleanup that is on trial.
    await waitFor(() => expect(fetchMock.mock.calls.some(
      ([u]) => String(u).startsWith("/api/chart/tick"))).toBe(true));

    unmount();

    expect(touchedAfterRemove).toEqual([]);
  });
});

/**
 * The TradingView view (asked for 2026-09-28): "add all of the selectable
 * instruments that you have available on trading view and also be able to
 * track back the chart for the past week or longer ... drawing on the graph".
 *
 * The broker chart cannot do any of that -- one symbol, 1000 candles, no
 * drawing tools -- so the tab offers TradingView's own chart beside it. What
 * is tested is what we hand the widget; the widget itself runs in TradingView's
 * iframe and is not ours to test.
 */
describe("the TradingView view", () => {
  // This repository's jsdom provides no `localStorage`; the choice is
  // remembered through a stand-in, as SplitPane's tests do.
  beforeEach(() => {
    const held = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => held.get(k) ?? null,
      setItem: (k: string, v: string) => { held.set(k, v); },
      removeItem: (k: string) => { held.delete(k); },
      clear: () => held.clear(),
      key: () => null,
      get length() { return held.size; },
    });
    // The widget lives for the whole page (see "kept alive" below), so each
    // test starts from a page that has not built one yet.
    resetTradingViewHost();
  });

  const widgetScript = () =>
    document.querySelector<HTMLScriptElement>('script[src*="tradingview.com"]');
  const widgetConfig = () => JSON.parse(widgetScript()?.innerHTML ?? "{}");

  it("opens on the broker chart", async () => {
    render(<ChartPanel />);

    expect(await screen.findByTestId("candle-chart")).toBeInTheDocument();
    expect(widgetScript()).toBeNull();
  });

  it("swaps the broker chart for TradingView's when chosen", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("candle-chart");

    await userEvent.click(screen.getByRole("button", { name: "TradingView" }));

    expect(await screen.findByTestId("tradingview-chart")).toBeInTheDocument();
    expect(screen.queryByTestId("candle-chart")).not.toBeInTheDocument();
    expect(widgetScript()?.src).toBe(
      "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js",
    );
  });

  it("lets the user pick any instrument and draw, starting on gold", async () => {
    render(<ChartPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
    await screen.findByTestId("tradingview-chart");

    const cfg = widgetConfig();
    expect(cfg.symbol).toBe("OANDA:XAUUSD");
    expect(cfg.allow_symbol_change).toBe(true);
    // The drawing tools live in the side toolbar.
    expect(cfg.hide_side_toolbar).toBe(false);
    expect(cfg.withdateranges).toBe(true);
  });

  it("hides the broker chart's timeframe buttons, which would do nothing", async () => {
    render(<ChartPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
    await screen.findByTestId("tradingview-chart");

    expect(screen.queryByRole("button", { name: "1H" })).not.toBeInTheDocument();
  });

  it("follows the app's theme", async () => {
    document.documentElement.setAttribute("data-theme", "light");
    render(<ChartPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
    await screen.findByTestId("tradingview-chart");

    expect(widgetConfig().theme).toBe("light");
    document.documentElement.removeAttribute("data-theme");
  });

  it("follows the machine when the app's theme is auto", async () => {
    // Found on the running app, 2026-09-28: "auto" on a light Mac was read as
    // dark, and a black chart sat in a white panel.
    document.documentElement.setAttribute("data-theme", "auto");
    vi.stubGlobal("matchMedia", (q: string) => ({
      matches: q === "(prefers-color-scheme: light)",
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    render(<ChartPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
    await screen.findByTestId("tradingview-chart");

    expect(widgetConfig().theme).toBe("light");
    document.documentElement.removeAttribute("data-theme");
  });

  it("remembers the choice when the tab is opened again", async () => {
    const first = render(<ChartPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
    await screen.findByTestId("tradingview-chart");
    first.unmount();

    render(<ChartPanel />);

    expect(await screen.findByTestId("tradingview-chart")).toBeInTheDocument();
  });

  it("offers the full tradingview.com chart, where a login keeps drawings", async () => {
    // The embedded widget cannot save drawings (2026-09-28): it has no setting
    // for it and its iframe is TradingView's origin, not ours.
    render(<ChartPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
    await screen.findByTestId("tradingview-chart");

    const link = screen.getByRole("link", { name: /open in tradingview/i });
    expect(link.getAttribute("href")).toBe(
      "https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD",
    );
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("goes back to the broker chart, hiding TradingView's rather than destroying it", async () => {
    // Was "and removes the widget" until 2026-09-28, when the owner asked for
    // TradingView drawings to survive leaving the page. Destroying the widget
    // is what lost them.
    render(<ChartPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
    await screen.findByTestId("tradingview-chart");

    await userEvent.click(screen.getByRole("button", { name: "Broker" }));

    expect(await screen.findByTestId("candle-chart")).toBeInTheDocument();
    expect(widgetScript()).not.toBeNull();
    expect(screen.getByTestId("tradingview-host").style.visibility).toBe("hidden");
  });

  /**
   * "if drawings are made on the trading view chart ... ensure they are
   * persistent" (2026-09-28). The widget exposes no drawing state (it sends
   * the page one message, openChartInPopup) and tradingview.com refuses to be
   * framed, so nothing can be saved and re-added. What can be done is to never
   * destroy the widget: its drawings then last as long as the page does.
   */
  describe("kept alive", () => {
    it("is built once, however often the tab is left and reopened", async () => {
      const first = render(<ChartPanel />);
      await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
      await screen.findByTestId("tradingview-chart");
      const script = widgetScript();
      first.unmount();                     // leaving the Chart tab unmounts it

      render(<ChartPanel />);              // coming back
      await screen.findByTestId("tradingview-chart");

      expect(document.querySelectorAll('script[src*="tradingview.com"]')).toHaveLength(1);
      expect(widgetScript()).toBe(script);
    });

    it("is shown again when the tab comes back", async () => {
      const first = render(<ChartPanel />);
      await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
      await screen.findByTestId("tradingview-chart");
      first.unmount();
      expect(screen.getByTestId("tradingview-host").style.visibility).toBe("hidden");

      render(<ChartPanel />);
      await screen.findByTestId("tradingview-chart");

      expect(screen.getByTestId("tradingview-host").style.visibility).toBe("visible");
    });

    it("keeps its drawings through a theme change rather than rebuilding", async () => {
      // Rebuilding is the only way to re-theme the widget, and it throws the
      // drawings away. The new theme applies from the next page load.
      document.documentElement.setAttribute("data-theme", "dark");
      render(<ChartPanel />);
      await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
      await screen.findByTestId("tradingview-chart");
      const script = widgetScript();

      document.documentElement.setAttribute("data-theme", "light");
      await new Promise((r) => setTimeout(r, 0));

      expect(widgetScript()).toBe(script);
      document.documentElement.removeAttribute("data-theme");
    });

    it("sits under dialogs, so an order dialog is never hidden behind it", async () => {
      render(<ChartPanel />);
      await userEvent.click(await screen.findByRole("button", { name: "TradingView" }));
      await screen.findByTestId("tradingview-chart");

      // DialogShell is z-50.
      expect(Number(screen.getByTestId("tradingview-host").style.zIndex)).toBeLessThan(50);
    });
  });
});

/**
 * "as close to real time as possible" (2026-09-28). Candles refresh every
 * 10 s; between refreshes the tick moves the forming bar, so the chart is as
 * live as the price in the header.
 */
describe("the forming candle", () => {
  it("moves with the bid between candle refreshes", async () => {
    barUpdates.length = 0;
    // On a 5m boundary, as MT5's 5m bars are.
    const bar = { ts: 1_750_000_200, open: 2430, high: 2432, low: 2429, close: 2431 };
    fetchMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/chart/candles")) {
        return { ok: true, status: 200, json: async () => [bar] };
      }
      if (url.startsWith("/api/chart/tick")) {
        return {
          ok: true, status: 200,
          json: async () => ({
            bid: 2433.5, ask: 2433.8, mid: 2433.65, spread: 0.3,
            spread_points: 30, timestamp: bar.ts + 30, source: "mt5",
          }),
        };
      }
      if (url.startsWith("/api/chart/overlays")) {
        return { ok: true, status: 200, json: async () => overlaysBody };
      }
      return { ok: true, status: 200, json: async () => [] };
    });
    render(<ChartPanel />);

    await waitFor(() => expect(barUpdates).toContainEqual({
      time: bar.ts, open: 2430, high: 2433.5, low: 2429, close: 2433.5,
    }));
  });
});

/**
 * "on the chart page it is now missing the open positions" (2026-09-28). The
 * trade had been placed by the VPS ("Node: Remote"), so this machine's own
 * database had no row for it and `/api/chart/trades` answered []. The Trading
 * tab and the Dashboard showed it because they read `/api/trading/trades`,
 * which adds what the broker holds that this machine has no record of. The
 * chart now reads that same list.
 */
describe("the open positions", () => {
  it("include a position the other node opened", async () => {
    const remote = {
      id: "vps-1", direction: "BUY", entry: 4151.03, lots: 0.03, sl: 4145.91,
      tp: null, pnl: 4.2, mt5_ticket: 2103198838, remote: true,
    };
    fetchMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/chart/candles")) {
        return { ok: true, status: 200, json: async () => CANDLES };
      }
      if (url.startsWith("/api/chart/overlays")) {
        return { ok: true, status: 200, json: async () => overlaysBody };
      }
      if (url.startsWith("/api/trading/trades")) {
        return { ok: true, status: 200, json: async () => [remote] };
      }
      // `/api/chart/trades`: this machine's database, which has no row.
      return { ok: true, status: 200, json: async () => [] };
    });
    render(<ChartPanel />);

    expect(await screen.findByText("2103198838")).toBeInTheDocument();
  });
});
