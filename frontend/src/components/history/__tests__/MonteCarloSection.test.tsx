import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MonteCarloSection } from "../internal/MonteCarloSection";
import { resetPolls } from "@/hooks/usePoll";

/** Resampled next-N-trade outcomes from the window's own trades (docs/todo/024). */
const RESULT = {
  trades: 120, horizon: 100, balance: 5000, paths: 5000, mean_trade: 8.5,
  final: { p5: 4400, p25: 5300, p50: 5850, p75: 6400, p95: 7200 },
  max_dd_pct: { p50: 6.2, p95: 14.8, p99: 21.3 },
  prob_dd: { "10": 0.231, "20": 0.034, "30": 0.004, "50": 0 },
  prob_loss: 0.12, prob_ruin: 0,
  band: [{ n: 0, p5: 5000, p50: 5000, p95: 5000 }, { n: 100, p5: 4400, p50: 5850, p95: 7200 }],
};

let body: Record<string, unknown>;
let urls: string[];

beforeEach(() => {
  resetPolls();
  urls = [];
  body = RESULT;
  vi.stubGlobal("fetch", vi.fn(async (u: string) => {
    urls.push(String(u));
    return { ok: true, status: 200, json: async () => body };
  }));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("MonteCarloSection", () => {
  it("asks for the window", async () => {
    render(<MonteCarloSection days={90} />);
    await waitFor(() => expect(urls[0]).toContain("/api/history/monte-carlo?days=90"));
  });

  it("shows the drawdown odds and the range of outcomes", async () => {
    render(<MonteCarloSection days={30} />);
    const box = await screen.findByTestId("monte-carlo");
    expect(box).toHaveTextContent("23.1%");   // P(drawdown >= 10%)
    expect(box).toHaveTextContent("3.4%");    // P(drawdown >= 20%)
    expect(box).toHaveTextContent("$5,850");  // median balance
    expect(box).toHaveTextContent("14.8%");   // 95th percentile drawdown
    expect(box).toHaveTextContent("120");     // trades it drew from
  });

  it("says why when there are too few trades", async () => {
    body = { trades: 12, note: "Needs at least 30 closed trades in the window; it has 12." };
    render(<MonteCarloSection days={30} />);
    expect(await screen.findByText(/at least 30 closed trades/)).toBeInTheDocument();
  });

  it("asks again with the horizon chosen", async () => {
    render(<MonteCarloSection days={30} />);
    await screen.findByTestId("monte-carlo");
    fireEvent.click(screen.getByRole("button", { name: "500 trades" }));
    await waitFor(() => expect(urls.some((u) => u.includes("horizon=500"))).toBe(true));
  });

  it("states its assumptions", async () => {
    render(<MonteCarloSection days={30} />);
    expect(await screen.findByText(/understated/i)).toBeInTheDocument();
  });
});
