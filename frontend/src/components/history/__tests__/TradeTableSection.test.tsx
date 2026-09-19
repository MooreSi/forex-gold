import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TradeTableSection } from "../internal/TradeTableSection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The deal-level trade table.
 *
 * Every assertion here is about a distinction the operator has to be able to
 * make from the table alone: a partial close from a single exit, a missing
 * opening deal from a scratched trade, a broker outage from a quiet month,
 * and "Max TP not measured yet" from "Max TP never reached".
 */

/** A closed BUY, opened and closed inside the window. */
function row(over: Record<string, unknown> = {}) {
  return {
    ticket: 501234,
    direction: "BUY",
    entry_price: 2412.55,
    exit_price: 2418.05,
    open_ts: 1757952000,
    close_ts: 1757955600,
    lots: 0.1,
    close_lots: [0.1],
    pnl: 55.0,
    fees: 1.2,
    pips: 55.0,
    duration_secs: 3600,
    order_type: "Market",
    pending_secs: null,
    reason: "TP hit",
    source: "GoldSignals",
    strategy: "Breakout",
    max_tp: "TP2",
    rr: 1.8,
    spread_points: 22.0,
    group: null,
    contract_size: 100,
    ...over,
  };
}

let body: { rows: unknown[]; error: string | null };
let fetchMock: ReturnType<typeof vi.fn>;
let urls: string[];

beforeEach(() => {
  resetPolls();
  urls = [];
  body = { rows: [row()], error: null };
  fetchMock = vi.fn(async (url: string) => {
    urls.push(String(url));
    return { ok: true, status: 200, json: async () => body };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("TradeTableSection", () => {
  it("asks the backend for the window it was given", async () => {
    render(<TradeTableSection days={90} />);
    await waitFor(() => expect(urls.length).toBeGreaterThan(0));
    expect(urls[0]).toContain("/api/history/trades?days=90");
  });

  it("renders one row per closed trade, with side, prices and P&L", async () => {
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    expect(tr).toHaveTextContent("BUY");
    expect(tr).toHaveTextContent("2412.55");
    expect(tr).toHaveTextContent("2418.05");
    expect(tr).toHaveTextContent("$55.00");
    expect(tr).toHaveTextContent("GoldSignals");
    expect(tr).toHaveTextContent("Breakout");
    expect(tr).toHaveTextContent("TP hit");
  });

  it("reads the close stamp as a broker timestamp, not a local one", async () => {
    // 1757955600 is UTC+3. Read raw it is 18:00 London, which is three hours
    // into the future and looks entirely plausible on a trade table.
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    expect(tr).toHaveTextContent("15:00");
    expect(tr).not.toHaveTextContent("18:00");
  });

  it("shows the breakdown when a position came off in pieces", async () => {
    body = { rows: [row({ lots: 0.3, close_lots: [0.1, 0.2] })], error: null };
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    expect(tr).toHaveTextContent("0.30 (0.10 + 0.20)");
  });

  it("shows a single lot size without a breakdown", async () => {
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    expect(tr).toHaveTextContent("0.10");
    expect(tr).not.toHaveTextContent("(0.10)");
  });

  it("marks pips and duration unknown when the open is outside the window", async () => {
    body = {
      rows: [row({ entry_price: 0, pips: null, duration_secs: null })],
      error: null,
    };
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    // Not "0.0 pips" and not "0m" -- either would read as a scratched trade.
    expect(tr).not.toHaveTextContent("0.0");
    expect(tr).not.toHaveTextContent("0m");
  });

  it("signs a winning pip count", async () => {
    render(<TradeTableSection days={30} />);
    expect(await screen.findByTestId("trade-501234")).toHaveTextContent("+55.0");
  });

  it("renders a held time in hours and minutes", async () => {
    body = { rows: [row({ duration_secs: 3600 * 5 + 60 * 7 })], error: null };
    render(<TradeTableSection days={30} />);
    expect(await screen.findByTestId("trade-501234")).toHaveTextContent("5h 7m");
  });

  it("says how long a limit order rested before it filled", async () => {
    body = {
      rows: [row({ order_type: "Limit", pending_secs: 1500 })],
      error: null,
    };
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    expect(tr).toHaveTextContent("Limit");
    expect(tr).toHaveTextContent("25m");
  });

  it("shows the sweep's placeholder Max TP rather than inventing a result", async () => {
    body = { rows: [row({ max_tp: "..." })], error: null };
    render(<TradeTableSection days={30} />);
    expect(await screen.findByTestId("trade-501234")).toHaveTextContent("...");
  });

  it("shows the fee cost as a breakdown beside the net P&L", async () => {
    // The fee is already inside `pnl`. It is shown so the operator can see
    // what the trade cost, not so it can be subtracted again.
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    expect(tr).toHaveTextContent("$1.20");
    expect(tr).toHaveTextContent("$55.00");
  });

  it("renders R:R against one", async () => {
    render(<TradeTableSection days={30} />);
    expect(await screen.findByTestId("trade-501234")).toHaveTextContent("1.80:1");
  });

  it("distinguishes a broker outage from a quiet window", async () => {
    body = { rows: [], error: "MT5 deal history is unavailable." };
    render(<TradeTableSection days={30} />);
    expect(await screen.findByText("No broker data")).toBeInTheDocument();
    expect(screen.getByText("MT5 deal history is unavailable.")).toBeInTheDocument();
    expect(screen.queryByTestId("trade-table")).not.toBeInTheDocument();
  });

  it("says a quiet window is quiet, without an error", async () => {
    body = { rows: [], error: null };
    render(<TradeTableSection days={30} />);
    expect(await screen.findByText("No trades closed in this window")).toBeInTheDocument();
    expect(screen.queryByText("No broker data")).not.toBeInTheDocument();
  });

  it("survives a response whose rows are not a list", async () => {
    body = { rows: {} as unknown as unknown[], error: null };
    render(<TradeTableSection days={30} />);
    expect(await screen.findByText("No trades closed in this window")).toBeInTheDocument();
  });
});
