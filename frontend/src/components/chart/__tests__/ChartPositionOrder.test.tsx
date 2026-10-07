/**
 * A position drawn on the Broker chart becomes an order (owner, 2026-09-28):
 * "once a forecasting position is placed on the chart ... convert this to an
 * order instead of having to manually enter the details".
 *
 * The position tool is the app's own, on the Broker chart. The TradingView
 * chart's long/short tool cannot be used: its drawings live inside a
 * cross-origin iframe the widget exposes no API for.
 *
 * What converting does is fill in the Trading tab's own dialogs. It sends
 * nothing: the review step and "Place this BUY" still stand, and the backend
 * still decides. Every order request in this file goes to a fetch fake.
 *
 * The stub maps bar index to pixels as x = 100 + 10 * index, and price to
 * pixels one-to-one; see src/test/chartStub.ts.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChartPanel } from "../ChartPanel";
import { resetPolls } from "@/hooks/usePoll";

vi.mock("lightweight-charts", async () => (await import("@/test/chartStub")).chartStub());

const TF = 300;
const T0 = 1_750_000_200;
const CANDLES = [0, 1, 2].map((i) => ({
  ts: T0 + i * TF, open: 4150, high: 4152, low: 4149, close: 4151,
}));

let stored: Record<string, unknown>[];
let fetchMock: ReturnType<typeof vi.fn>;

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body });

const position = (id: number, entry: number, stop: number, target: number) => ({
  id, symbol: "XAUUSD", kind: "position", created_at: 1, updated_at: 1,
  points: [
    { time: T0, price: entry },
    { time: T0 + 2 * TF, price: stop },
    { time: T0 + 2 * TF, price: target },
  ],
});

beforeEach(() => {
  resetPolls();
  stored = [position(7, 4150, 4140, 4170)];
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.startsWith("/api/trading/halt")) {
      return ok({ halted: false, reason: null, market_closed: false, circuit_breaker: null });
    }
    if (url.startsWith("/api/chart/drawings")) {
      if (method === "GET") return ok(structuredClone(stored));
      if (method === "POST") {
        const body = JSON.parse(String(init?.body));
        return ok({ id: 50, created_at: 1, updated_at: 1, ...body });
      }
    }
    if (url.startsWith("/api/trading/orders")) return ok({ mt5_ticket: 1 });
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

const sent = (prefix: string, method = "POST") =>
  fetchMock.mock.calls
    .filter(([u, i]) => String(u).startsWith(prefix) && (i?.method ?? "GET") === method)
    .map(([u, i]) => ({ url: String(u), body: JSON.parse(String(i?.body)) }));

async function selectPosition(id = 7) {
  const body = await screen.findByTestId(`drawing-position-${id}`);
  fireEvent.mouseDown(body, { clientX: 105, clientY: 4160 });
  fireEvent.mouseUp(window, { clientX: 105, clientY: 4160 });
}

describe("drawing a position", () => {
  it("takes the entry, the stop and the target, in that order", async () => {
    stored = [];
    render(<ChartPanel />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Position" })).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Position" }));
    const surface = screen.getByTestId("drawing-surface");
    fireEvent.mouseDown(surface, { clientX: 100, clientY: 4150 });
    fireEvent.mouseDown(surface, { clientX: 120, clientY: 4140 });
    fireEvent.mouseDown(surface, { clientX: 120, clientY: 4170 });

    await waitFor(() => expect(sent("/api/chart/drawings")).toHaveLength(1));
    const body = sent("/api/chart/drawings")[0].body;
    expect(body.kind).toBe("position");
    expect(body.points.map((p: { price: number }) => p.price)).toEqual([4150, 4140, 4170]);
  });

  it("labels its direction and reward-to-risk", async () => {
    render(<ChartPanel />);
    const shape = await screen.findByTestId("drawing-position-7");
    expect(within(shape as unknown as HTMLElement).getByText(/LONG/)).toBeInTheDocument();
    expect(within(shape as unknown as HTMLElement).getByText(/R:R 2/)).toBeInTheDocument();
  });
});

describe("turning a selected position into an order", () => {
  it("offers nothing until a position is selected", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("drawing-position-7");
    expect(screen.queryByRole("button", { name: /market from position/i })).not.toBeInTheDocument();
  });

  it("fills the market order with the direction, stop and target", async () => {
    render(<ChartPanel />);
    await selectPosition();

    const button = await screen.findByRole("button", { name: /market from position/i });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "BUY" }).getAttribute("aria-pressed")).toBe("true");
    expect(within(dialog).getByLabelText(/Stop loss/)).toHaveValue("4140");
    expect(within(dialog).getByLabelText(/Take profit/)).toHaveValue("4170");
    // Lots stay blank: the risk settings size it, as with a typed order.
    expect(within(dialog).getByLabelText(/Lots/)).toHaveValue("");
    expect(sent("/api/trading/orders")).toEqual([]);
  });

  it("still needs the review and the confirmation, and sends what was drawn", async () => {
    render(<ChartPanel />);
    await selectPosition();
    const button = await screen.findByRole("button", { name: /market from position/i });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    await userEvent.click(await screen.findByRole("button", { name: /Review BUY/ }));
    expect(screen.getByTestId("order-summary").textContent).toContain("take profit at 4170.00");
    expect(sent("/api/trading/orders")).toEqual([]);

    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));

    await waitFor(() => expect(sent("/api/trading/orders/market")).toHaveLength(1));
    // 2026-10-07 (owner): no strategy chosen is a single take profit, sent
    // as orb_fixed, not the backend's null (Scale Out + Breakeven).
    expect(sent("/api/trading/orders/market")[0].body).toEqual({
      direction: "BUY", lot_size: null, stop_loss: 4140, take_profit: 4170,
      strategy: "orb_fixed",
    });
  });

  it("fills a limit order resting at the drawn entry", async () => {
    stored = [position(7, 4150, 4160, 4120)];
    render(<ChartPanel />);
    await selectPosition();

    const button = await screen.findByRole("button", { name: /limit from position/i });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "SELL" }).getAttribute("aria-pressed")).toBe("true");
    expect(within(dialog).getByLabelText("Entry zone low")).toHaveValue("4150");
    expect(within(dialog).getByLabelText("Entry zone high")).toHaveValue("4150");
    expect(within(dialog).getByLabelText("Stop loss")).toHaveValue("4160");
    // 2026-10-07 (owner): one "Take profit" field replaced TP1-TP8.
    expect(within(dialog).getByLabelText("Take profit")).toHaveValue("4120");
  });

  it("keeps the position when Delete is pressed inside its order dialog", async () => {
    // The position stays selected while its dialog is open, and the chart
    // deletes the selected drawing on Delete or Backspace.
    render(<ChartPanel />);
    await selectPosition();
    const button = await screen.findByRole("button", { name: /market from position/i });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    const dialog = await screen.findByRole("dialog");
    fireEvent.keyDown(within(dialog).getByRole("button", { name: "BUY" }), { key: "Delete" });

    expect(fetchMock.mock.calls.filter(([, i]) => i?.method === "DELETE")).toEqual([]);
  });

  it("is refused, with the reason, for a position that is not a trade", async () => {
    // Target on the stop's side: neither a BUY nor a SELL.
    stored = [position(7, 4150, 4140, 4145)];
    render(<ChartPanel />);
    await selectPosition();

    const button = await screen.findByRole("button", { name: /market from position/i });
    expect(button).toBeDisabled();
    expect(button.getAttribute("title")).toMatch(/opposite sides of the entry/);
  });
});
