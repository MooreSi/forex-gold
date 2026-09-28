/**
 * Drawing on the Broker chart (docs/todo/011).
 *
 * The owner, 2026-09-28: "the drawings do not survive a page load so this
 * needs fixing". The embedded TradingView chart cannot keep them; these are
 * stored by the app, so what is tested is the round trip: what is drawn is
 * sent to the API as (time, price), and what the API holds is drawn again.
 *
 * The stub maps bar index to pixels as x = 100 + 10 * index, and price to
 * pixels one-to-one; see src/test/chartStub.ts.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChartPanel } from "../ChartPanel";
import { resetPolls } from "@/hooks/usePoll";
import { touchedAfterRemove } from "@/test/chartStub";

vi.mock("lightweight-charts", async () => (await import("@/test/chartStub")).chartStub());

const TF = 300; // the tab opens on 5m
const T0 = 1_750_000_200; // on a 5m boundary
const CANDLES = [0, 1, 2].map((i) => ({
  ts: T0 + i * TF, open: 4150, high: 4152, low: 4149, close: 4151,
}));

let stored: Record<string, unknown>[];
let fetchMock: ReturnType<typeof vi.fn>;
let nextId: number;

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body });

beforeEach(() => {
  resetPolls();
  touchedAfterRemove.length = 0;
  nextId = 50;
  stored = [{
    id: 7, symbol: "XAUUSD", kind: "trend", created_at: 1, updated_at: 1,
    points: [{ time: T0, price: 4150 }, { time: T0 + 2 * TF, price: 4160 }],
  }];
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.startsWith("/api/chart/drawings")) {
      // Copies, as a real response is: handing back the fake's own array
      // lets the component's state and the "server" share one object.
      if (method === "GET") return ok(structuredClone(stored));
      if (method === "POST") {
        const body = JSON.parse(String(init?.body));
        const saved = { id: nextId++, created_at: 1, updated_at: 1, ...body };
        stored.push(saved);
        return ok(structuredClone(saved));
      }
      const id = Number(url.split("/").pop());
      if (method === "PUT") {
        const body = JSON.parse(String(init?.body));
        const d = stored.find((x) => x.id === id)!;
        d.points = body.points;
        return ok(structuredClone(d));
      }
      if (method === "DELETE") {
        stored = stored.filter((x) => x.id !== id);
        return ok({ deleted: id });
      }
    }
    if (url.startsWith("/api/chart/candles")) return ok(CANDLES);
    if (url.startsWith("/api/chart/overlays")) {
      return ok({ timeframe: "5m", count: 3, emas: {}, rsi: [], fvgs: [] });
    }
    if (url.startsWith("/api/chart/tick")) return ok(null);
    return ok([]);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const sent = (method: string) =>
  fetchMock.mock.calls
    .filter(([u, init]) => String(u).startsWith("/api/chart/drawings")
      && (init?.method ?? "GET") === method)
    .map(([u, init]) => ({ url: String(u), body: init?.body ? JSON.parse(String(init.body)) : null }));

describe("drawings that were saved", () => {
  it("are asked for, for the chart's symbol", async () => {
    render(<ChartPanel />);
    await waitFor(() => expect(sent("GET").length).toBeGreaterThan(0));
    expect(sent("GET")[0].url).toBe("/api/chart/drawings?symbol=XAUUSD");
  });

  it("are drawn where their time and price are, not at stored pixels", async () => {
    render(<ChartPanel />);
    const line = await screen.findByTestId("drawing-trend-7");
    // Bar 0 -> x 100, bar 2 -> x 120; price is y one-to-one in the stub.
    expect(line.getAttribute("x1")).toBe("100");
    expect(line.getAttribute("y1")).toBe("4150");
    expect(line.getAttribute("x2")).toBe("120");
    expect(line.getAttribute("y2")).toBe("4160");
  });

  it("are drawn between two bars when their time falls between them", async () => {
    // A line drawn on 5m, viewed on 1H: its ends are between hourly bars.
    // Found in the running app on 2026-09-28, drawn at the left edge instead.
    stored = [{
      id: 8, symbol: "XAUUSD", kind: "trend", created_at: 1, updated_at: 1,
      points: [{ time: T0 + TF / 2, price: 4150 }, { time: T0 + 1.5 * TF, price: 4160 }],
    }];
    render(<ChartPanel />);
    const line = await screen.findByTestId("drawing-trend-8");
    expect(line.getAttribute("x1")).toBe("105");
    expect(line.getAttribute("x2")).toBe("115");
  });

  it("are drawn nowhere when none were saved", async () => {
    stored = [];
    render(<ChartPanel />);
    await screen.findByTestId("candle-chart");
    await waitFor(() => expect(sent("GET").length).toBeGreaterThan(0));
    expect(screen.queryByTestId(/^drawing-/)).not.toBeInTheDocument();
  });
});

describe("drawing a new one", () => {
  it("saves a trend line from two clicks, as time and price", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("drawing-trend-7");

    await userEvent.click(screen.getByRole("button", { name: "Trend line" }));
    const surface = screen.getByTestId("drawing-surface");
    fireEvent.mouseDown(surface, { clientX: 110, clientY: 4155 });  // bar 1
    fireEvent.mouseDown(surface, { clientX: 150, clientY: 4170 });  // bar 5, past the last

    await waitFor(() => expect(sent("POST")).toHaveLength(1));
    expect(sent("POST")[0].body).toEqual({
      symbol: "XAUUSD",
      kind: "trend",
      points: [{ time: T0 + TF, price: 4155 }, { time: T0 + 5 * TF, price: 4170 }],
    });
    expect(await screen.findByTestId("drawing-trend-50")).toBeInTheDocument();
  });

  it("saves a horizontal level from one click", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("drawing-trend-7");

    await userEvent.click(screen.getByRole("button", { name: "Horizontal level" }));
    fireEvent.mouseDown(screen.getByTestId("drawing-surface"), { clientX: 110, clientY: 4158 });

    await waitFor(() => expect(sent("POST")).toHaveLength(1));
    expect(sent("POST")[0].body.kind).toBe("hline");
    expect(sent("POST")[0].body.points).toEqual([{ time: T0 + TF, price: 4158 }]);
  });

  it("draws a Fibonacci retracement's levels between the two clicks", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("drawing-trend-7");

    await userEvent.click(screen.getByRole("button", { name: "Fibonacci retracement" }));
    const surface = screen.getByTestId("drawing-surface");
    fireEvent.mouseDown(surface, { clientX: 100, clientY: 4100 });
    fireEvent.mouseDown(surface, { clientX: 120, clientY: 4200 });

    const fib = await screen.findByTestId("drawing-fib-50");
    const level618 = fib.querySelector('[data-level="0.618"]');
    expect(Number(level618?.getAttribute("y1"))).toBeCloseTo(4200 - 61.8, 6);
  });

  it("goes back to the pointer once a drawing is finished", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("drawing-trend-7");

    const tool = screen.getByRole("button", { name: "Horizontal level" });
    await userEvent.click(tool);
    expect(tool.getAttribute("aria-pressed")).toBe("true");
    fireEvent.mouseDown(screen.getByTestId("drawing-surface"), { clientX: 110, clientY: 4158 });

    await waitFor(() => expect(tool.getAttribute("aria-pressed")).toBe("false"));
    // With no tool, the chart underneath gets the mouse again (pan and zoom).
    expect(screen.queryByTestId("drawing-surface")).not.toBeInTheDocument();
  });

  it("is abandoned with Escape, and nothing is saved", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("drawing-trend-7");

    await userEvent.click(screen.getByRole("button", { name: "Trend line" }));
    fireEvent.mouseDown(screen.getByTestId("drawing-surface"), { clientX: 110, clientY: 4155 });
    fireEvent.keyDown(window, { key: "Escape" });

    expect(screen.queryByTestId("drawing-surface")).not.toBeInTheDocument();
    expect(sent("POST")).toHaveLength(0);
  });
});

describe("changing one", () => {
  it("deletes the selected drawing with the Delete key", async () => {
    render(<ChartPanel />);
    fireEvent.mouseDown(await screen.findByTestId("drawing-trend-7"), { clientX: 110, clientY: 4155 });
    fireEvent.mouseUp(window, { clientX: 110, clientY: 4155 });

    fireEvent.keyDown(window, { key: "Delete" });

    await waitFor(() => expect(sent("DELETE")).toHaveLength(1));
    expect(sent("DELETE")[0].url).toBe("/api/chart/drawings/7");
    await waitFor(() => expect(screen.queryByTestId("drawing-trend-7")).not.toBeInTheDocument());
  });

  it("does not delete anything when nothing is selected", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("drawing-trend-7");

    fireEvent.keyDown(window, { key: "Delete" });

    expect(sent("DELETE")).toHaveLength(0);
  });

  it("saves the new end when a handle is dragged", async () => {
    render(<ChartPanel />);
    fireEvent.mouseDown(await screen.findByTestId("drawing-trend-7"), { clientX: 110, clientY: 4155 });
    fireEvent.mouseUp(window, { clientX: 110, clientY: 4155 });

    const handle = await screen.findByTestId("drawing-handle-7-1");
    fireEvent.mouseDown(handle, { clientX: 120, clientY: 4160 });
    fireEvent.mouseMove(window, { clientX: 130, clientY: 4165 });
    fireEvent.mouseUp(window, { clientX: 130, clientY: 4165 });

    await waitFor(() => expect(sent("PUT")).toHaveLength(1));
    expect(sent("PUT")[0].url).toBe("/api/chart/drawings/7");
    expect(sent("PUT")[0].body.points).toEqual([
      { time: T0, price: 4150 },
      { time: T0 + 3 * TF, price: 4165 },
    ]);
  });

  it("moves the whole drawing when its body is dragged", async () => {
    render(<ChartPanel />);
    const line = await screen.findByTestId("drawing-trend-7");

    fireEvent.mouseDown(line, { clientX: 110, clientY: 4155 });
    fireEvent.mouseMove(window, { clientX: 120, clientY: 4150 });  // +1 bar, -5 price
    fireEvent.mouseUp(window, { clientX: 120, clientY: 4150 });

    await waitFor(() => expect(sent("PUT")).toHaveLength(1));
    expect(sent("PUT")[0].body.points).toEqual([
      { time: T0 + TF, price: 4145 },
      { time: T0 + 3 * TF, price: 4155 },
    ]);
  });

  it("does not save a click that did not move anything", async () => {
    render(<ChartPanel />);
    const line = await screen.findByTestId("drawing-trend-7");

    fireEvent.mouseDown(line, { clientX: 110, clientY: 4155 });
    fireEvent.mouseUp(window, { clientX: 110, clientY: 4155 });

    expect(sent("PUT")).toHaveLength(0);
  });
});

describe("leaving the tab", () => {
  it("touches nothing on the chart after it has been removed", async () => {
    const { unmount } = render(<ChartPanel />);
    await screen.findByTestId("drawing-trend-7");

    unmount();

    expect(touchedAfterRemove).toEqual([]);
  });
});
