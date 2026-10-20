import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TradeTableSection } from "../internal/TradeTableSection";
import { ExcursionSummarySection } from "../internal/ExcursionSummarySection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * How far each trade went against us (MAE) and in our favour (MFE), and how
 * much of the best price it banked (docs/todo/024). Measured by the backend
 * from candles; a blank is "not measured yet" and "n/a" is "could not be".
 */
function row(over: Record<string, unknown> = {}) {
  return {
    ticket: 501234, direction: "BUY", entry_price: 2412.55, exit_price: 2418.05,
    open_ts: 1757952000, close_ts: 1757955600, lots: 0.1, close_lots: [0.1],
    pnl: 55.0, fees: 1.2, pips: 55.0, duration_secs: 3600, order_type: "Market",
    pending_secs: null, reason: "TP hit", source: "GoldSignals", strategy: "Breakout",
    max_tp: "TP2", rr: 1.8, spread_points: 22.0, group: null, contract_size: 100,
    mae_pips: 12.5, mfe_pips: 80.0, mae_usd: 12.5, mfe_usd: 80.0, capture: 0.6875,
    excursion_res: "M1",
    ...over,
  };
}

let body: { rows: unknown[]; error: string | null };

beforeEach(() => {
  resetPolls();
  body = { rows: [row()], error: null };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => body })));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("excursion columns", () => {
  it("shows MAE, MFE and capture", async () => {
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    expect(tr).toHaveTextContent("-12.5");
    expect(tr).toHaveTextContent("+80.0");
    expect(tr).toHaveTextContent("69%");
  });

  it("says n/a for a trade that could not be measured", async () => {
    body = { rows: [row({ mae_pips: null, mfe_pips: null, mae_usd: null, mfe_usd: null,
                          capture: null, excursion_res: "none" })], error: null };
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    expect(tr).toHaveTextContent("n/a");
  });

  it("does not invent a zero for a trade not measured yet", async () => {
    body = { rows: [row({ mae_pips: null, mfe_pips: null, mae_usd: null, mfe_usd: null,
                          capture: null, excursion_res: null, pips: null, entry_price: 0 })],
             error: null };
    render(<TradeTableSection days={30} />);
    const tr = await screen.findByTestId("trade-501234");
    expect(tr).not.toHaveTextContent("0.0");
    expect(tr).not.toHaveTextContent("n/a");
  });
});

describe("ExcursionSummarySection", () => {
  it("summarises winners' heat and losers' missed profit", async () => {
    body = { rows: [
      row({ ticket: 1, pnl: 10, mae_pips: 10, mfe_pips: 50, capture: 0.4 }),
      row({ ticket: 2, pnl: 20, mae_pips: 30, mfe_pips: 60, capture: 0.6 }),
      row({ ticket: 3, pnl: 30, mae_pips: 20, mfe_pips: 70, capture: 0.8 }),
      row({ ticket: 4, pnl: -10, mae_pips: 50, mfe_pips: 15, capture: -1 }),
    ], error: null };
    render(<ExcursionSummarySection days={30} />);
    const box = await screen.findByTestId("excursion-summary");
    // Median MAE of the three winners: 20 pips. Median MFE of the loser: 15.
    expect(box).toHaveTextContent("20.0 pips");
    expect(box).toHaveTextContent("15.0 pips");
    // Median capture of the winners: 60%.
    expect(box).toHaveTextContent("60%");
    expect(box).toHaveTextContent("4 of 4");
  });

  it("stays out of the way when nothing is measured yet", async () => {
    body = { rows: [row({ mae_pips: null, mfe_pips: null, capture: null, excursion_res: null })],
             error: null };
    render(<ExcursionSummarySection days={30} />);
    await waitFor(() => expect(screen.queryByTestId("excursion-summary")).toBeNull());
  });
});
