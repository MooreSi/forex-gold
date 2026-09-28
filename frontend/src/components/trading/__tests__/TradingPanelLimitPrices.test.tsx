/**
 * The Trading tab's limit order gets its example prices from the header's
 * live price (2026-09-28). The header's poll is the one the whole shell
 * already runs, so this adds no request of its own when the app is running.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TradingPanel } from "../TradingPanel";
import { resetPolls } from "@/hooks/usePoll";

vi.mock("lightweight-charts", async () => (await import("@/test/chartStub")).chartStub());

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body });

beforeEach(() => {
  resetPolls();
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.startsWith("/api/system/header")) {
      return ok({ tick: { bid: 4150.4, ask: 4150.61, mid: 4150.5, spread: 0.21,
                          spread_points: 21, timestamp: 1, source: "mt5" } });
    }
    if (url.startsWith("/api/trading/halt")) {
      return ok({ halted: false, reason: null, market_closed: false, circuit_breaker: null });
    }
    return ok([]);
  }));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

it("gives the limit order's example prices from the live price", async () => {
  render(<TradingPanel />);
  const button = screen.getByRole("button", { name: /limit order/i });
  await waitFor(() => expect(button).toBeEnabled());

  await userEvent.click(button);

  await waitFor(() => expect(
    screen.getByLabelText("Entry zone low").getAttribute("placeholder"),
  ).toBe("4148.00"));
});
