/**
 * Market and Limit order from the Chart tab (owner, 2026-09-28): "above open
 * positions add two buttons for Market Order and Limit Order ... as i can draw
 * on the chart and then decide to place a manual order".
 *
 * These open the SAME dialogs the Trading tab uses: the same review step, the
 * same "Place this BUY" confirmation, the same backend refusal. Nothing about
 * how an order is built or sent is new here, and no test in this file sends
 * one; that is pinned below. The dialogs' own behaviour is tested in
 * trading/__tests__/PlaceOrderDialog.test.tsx and PlaceLimitOrderDialog.test.tsx.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChartPanel } from "../ChartPanel";
import { resetPolls } from "@/hooks/usePoll";

vi.mock("lightweight-charts", async () => (await import("@/test/chartStub")).chartStub());

let halt: Record<string, unknown>;
let fetchMock: ReturnType<typeof vi.fn>;

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body });

beforeEach(() => {
  resetPolls();
  halt = { halted: false, reason: null, market_closed: false, circuit_breaker: null };
  fetchMock = vi.fn(async (url: string) => {
    if (url.startsWith("/api/trading/halt")) return ok(halt);
    if (url.startsWith("/api/chart/candles")) {
      return ok([{ ts: 1_750_000_200, open: 1, high: 2, low: 0.5, close: 1.5 }]);
    }
    if (url.startsWith("/api/chart/overlays")) {
      return ok({ timeframe: "5m", count: 1, emas: {}, rsi: [], fvgs: [] });
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

/** The halt poll has answered and the answer has rendered. Needed before
 *  asserting a button is ENABLED: it is enabled before the halt loads too. */
async function haltRendered() {
  await waitFor(() => expect(
    fetchMock.mock.calls.some(([u]) => String(u).startsWith("/api/trading/halt"))).toBe(true));
  await act(() => new Promise((r) => setTimeout(r, 50)));
}

const orderCalls = () =>
  fetchMock.mock.calls.filter(([u]) => String(u).startsWith("/api/trading/orders"));

describe("order buttons on the Chart tab", () => {
  it("opens the market order dialog", async () => {
    render(<ChartPanel />);
    await waitFor(() => expect(screen.getByRole("button", { name: /market order/i })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: /market order/i }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Market order")).toBeInTheDocument();
  });

  it("opens the limit order dialog", async () => {
    render(<ChartPanel />);
    await waitFor(() => expect(screen.getByRole("button", { name: /limit order/i })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: /limit order/i }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Limit order")).toBeInTheDocument();
  });

  it("sends nothing by being opened: the dialog's own review and confirm still stand", async () => {
    render(<ChartPanel />);
    await waitFor(() => expect(screen.getByRole("button", { name: /market order/i })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: /market order/i }));
    await screen.findByRole("dialog");

    expect(orderCalls()).toEqual([]);
  });

  // Owner, 2026-10-05: a manual Limit order sits outside the daily pause and
  // any other paused trading, so it stays selectable. The backend already
  // places it through a pause (manual_limit_order checks neither the pause nor
  // the breaker; resting_revalidation exempts channel "Manual"). This replaced
  // the assertion that Limit was disabled with Market.
  //
  // Owner, 2026-10-07: "a market or limit order can bypass any paused
  // trading" -- Market too. `open_trade` now skips the pause and the breaker
  // for the dialog's "manual_market" source. These three replaced the
  // assertions that Market was disabled by a halt and by a tripped breaker.
  it("keeps Market selectable when trading is halted", async () => {
    halt = { ...halt, halted: true, reason: "Trading paused by the operator." };
    render(<ChartPanel />);
    await haltRendered();

    expect(screen.getByRole("button", { name: /market order/i })).toBeEnabled();
  });

  it("keeps Limit selectable when trading is halted", async () => {
    halt = { ...halt, halted: true, reason: "Daily goal secured: +$30.00" };
    render(<ChartPanel />);
    await haltRendered();

    expect(screen.getByRole("button", { name: /market order/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /limit order/i })).toBeEnabled();
  });

  it("keeps both selectable when the circuit breaker has tripped", async () => {
    halt = { ...halt, circuit_breaker: { is_active: true, remaining_secs: 600, consec_losses: 3 } };
    render(<ChartPanel />);
    await haltRendered();

    expect(screen.getByRole("button", { name: /market order/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /limit order/i })).toBeEnabled();
  });

  it("still disables Limit when the market is closed", async () => {
    halt = { ...halt, market_closed: true };
    render(<ChartPanel />);

    const limit = screen.getByRole("button", { name: /limit order/i });
    await waitFor(() => expect(limit).toBeDisabled());
    expect(limit.getAttribute("title")).toBe("The market is closed for the week.");
  });

  it("is disabled when the market is closed", async () => {
    halt = { ...halt, market_closed: true };
    render(<ChartPanel />);

    const market = screen.getByRole("button", { name: /market order/i });
    await waitFor(() => expect(market).toBeDisabled());
    expect(market.getAttribute("title")).toBe("The market is closed for the week.");
  });

  it("gives the limit order's example prices from the chart's live price", async () => {
    const base = fetchMock.getMockImplementation() as (u: string, i?: RequestInit) => Promise<unknown>;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => (
      url.startsWith("/api/chart/tick")
        ? ok({ bid: 4150.4, ask: 4150.61, mid: 4150.5, spread: 0.21,
               spread_points: 21, timestamp: 1_750_000_230, source: "mt5" })
        : base(url, init)
    ));
    render(<ChartPanel />);
    await waitFor(() => expect(screen.getByRole("button", { name: /limit order/i })).toBeEnabled());
    await waitFor(() => expect(
      fetchMock.mock.calls.some(([u]) => String(u).startsWith("/api/chart/tick")),
    ).toBe(true));

    await userEvent.click(screen.getByRole("button", { name: /limit order/i }));

    await waitFor(() => expect(
      screen.getByLabelText("Entry zone low").getAttribute("placeholder"),
    ).toBe("4148.00"));
  });

  it("asks the same halt endpoint the Trading tab asks", async () => {
    render(<ChartPanel />);
    await waitFor(() => expect(
      fetchMock.mock.calls.some(([u]) => String(u) === "/api/trading/halt"),
    ).toBe(true));
  });
});
