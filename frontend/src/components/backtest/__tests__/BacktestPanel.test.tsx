import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BacktestPanel } from "../BacktestPanel";
import type { BacktestOptions, BacktestResult, StrategyResult } from "@/api/types";

const OPTIONS: BacktestOptions = {
  strategies: [
    { key: "scale_out", name: "Scale Out" },
    { key: "be_runner", name: "BE Runner" },
  ],
  templates: [
    { name: "Trail Runner", supported: true },
    { name: "Grid Runner", supported: false, reason: "grid legs are not simulated on candles" },
  ],
  timeframes: ["M1", "M5", "H1"],
  granularities: ["candles", "ticks"],
  min_trades_per_side: 30,
  broker_tz_offset: 10800,
};

function stats(over: Partial<StrategyResult> = {}): StrategyResult {
  return {
    strategy: "scale_out", trades: 12, wins: 7, losses: 5, win_rate: 58.3,
    total_pnl: 145.2, total_commission: 8.4, avg_win: 42, avg_loss: -21,
    profit_factor: 1.9, max_drawdown_pct: 6.2, sharpe: 0.8, final_balance: 1145.2,
    equity_curve: [1000, 1145.2], unsupported_reason: "", ...over,
  };
}

const RESULT: BacktestResult = {
  results: [stats()],
  filtered: {
    total: 40, valid: 30, out_of_window: 8, zero_sl: 1, point_entry: 1,
    wide_sl: 0, bad_tp: 0,
    candle_start: "2026-05-16 00:00 UTC", candle_end: "2026-06-15 00:00 UTC",
    signal_start: "2026-05-01 00:00 UTC", signal_end: "2026-06-15 00:00 UTC",
  },
  signals_loaded: 40, candles_loaded: 8640, granularity: "candles", note: null,
};

let fetchMock: ReturnType<typeof vi.fn>;
let runBody: BacktestResult;

beforeEach(() => {
  runBody = RESULT;
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/backtest/options") {
      return { ok: true, status: 200, json: async () => OPTIONS };
    }
    if (url === "/api/backtest/run" && init?.method === "POST") {
      return { ok: true, status: 200, json: async () => runBody };
    }
    return { ok: false, status: 404, statusText: "", json: async () => ({}) };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const sentBody = () =>
  JSON.parse(fetchMock.mock.calls.find((c) => c[0] === "/api/backtest/run")![1].body);

describe("the form", () => {
  it("offers the strategies the backend listed", async () => {
    render(<BacktestPanel />);

    expect(await screen.findByRole("button", { name: "Scale Out" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "BE Runner" })).toBeInTheDocument();
  });

  it("will not run until a strategy is chosen, and says why", async () => {
    render(<BacktestPanel />);

    const run = await screen.findByRole("button", { name: /Run backtest/ });
    expect(run).toBeDisabled();
    expect(run).toHaveAttribute("title", expect.stringContaining("at least one"));
  });

  it("disables a template that cannot be simulated, and gives its reason", async () => {
    // The whole point: a template that would return zeros is not offered as if
    // it were a fair comparison.
    render(<BacktestPanel />);

    const grid = await screen.findByRole("button", { name: /Grid Runner/ });
    expect(grid).toBeDisabled();
    expect(grid).toHaveAttribute("title", "grid legs are not simulated on candles");
  });

  it("offers a template that can be simulated", async () => {
    render(<BacktestPanel />);

    expect(await screen.findByRole("button", { name: "Trail Runner" })).toBeEnabled();
  });

  it("sends the chosen strategies and the cost assumptions", async () => {
    render(<BacktestPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Scale Out" }));

    const spread = screen.getByLabelText("Spread");
    await userEvent.clear(spread);
    await userEvent.type(spread, "1.25");
    await userEvent.click(screen.getByRole("button", { name: /Run backtest/ }));

    await waitFor(() => expect(sentBody().strategies).toEqual(["scale_out"]));
    expect(sentBody().spread_pts).toBe(1.25);
    expect(sentBody().commission_per_lot).toBe(7);
  });

  it("sends the timeframe and granularity that were picked", async () => {
    render(<BacktestPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Scale Out" }));
    await userEvent.selectOptions(screen.getByLabelText("Timeframe"), "H1");
    await userEvent.selectOptions(screen.getByLabelText("Data"), "ticks");

    await userEvent.click(screen.getByRole("button", { name: /Run backtest/ }));

    await waitFor(() => expect(sentBody().timeframe).toBe("H1"));
    expect(sentBody().granularity).toBe("ticks");
  });
});

describe("the results", () => {
  it("shows one row per strategy with its numbers", async () => {
    render(<BacktestPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Scale Out" }));
    await userEvent.click(screen.getByRole("button", { name: /Run backtest/ }));

    expect(await screen.findByText("+$145.20")).toBeInTheDocument();
    expect(screen.getByText("58.3%")).toBeInTheDocument();
    expect(screen.getByText("1.90")).toBeInTheDocument();
  });

  it("renders a refused strategy as a refusal, not as zeros", async () => {
    // Zeros beside a row with a real drawdown read as an argument FOR the
    // strategy that was never simulated. This is the assertion that stops it.
    runBody = {
      ...RESULT,
      results: [stats({
        strategy: "template:Grid Runner", trades: 0, wins: 0, losses: 0,
        win_rate: 0, total_pnl: 0, profit_factor: 0, max_drawdown_pct: 0,
        final_balance: 1000, equity_curve: [],
        unsupported_reason: "grid legs are not simulated on candles",
      })],
    };
    render(<BacktestPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Scale Out" }));
    await userEvent.click(screen.getByRole("button", { name: /Run backtest/ }));

    const cell = await screen.findByTestId("unsupported-template:Grid Runner");
    expect(cell).toHaveTextContent("not simulated");
    expect(cell).toHaveTextContent("grid legs are not simulated on candles");
    expect(screen.queryByText("0.00")).toBeNull();
  });

  it("shows how many signals were dropped and why", async () => {
    render(<BacktestPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Scale Out" }));
    await userEvent.click(screen.getByRole("button", { name: /Run backtest/ }));

    expect(await screen.findByText(/signals walked/)).toBeInTheDocument();
    expect(screen.getByText("outside the candle window")).toBeInTheDocument();
    expect(screen.getByText("no stop loss")).toBeInTheDocument();
  });

  it("does not list a drop reason that did not happen", async () => {
    // Negative control: a static list of every reason would make a clean run
    // look like a filtered one.
    render(<BacktestPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Scale Out" }));
    await userEvent.click(screen.getByRole("button", { name: /Run backtest/ }));

    await screen.findByText(/signals walked/);
    expect(screen.queryByText("stop too wide")).toBeNull();
  });

  it("says there was nothing to walk rather than showing an empty table", async () => {
    runBody = { ...RESULT, results: [], note: "No recorded signals to walk." };
    render(<BacktestPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Scale Out" }));
    await userEvent.click(screen.getByRole("button", { name: /Run backtest/ }));

    expect(await screen.findByText("Nothing to walk")).toBeInTheDocument();
  });
});

describe("when the backend refuses", () => {
  it("shows the reason it gave", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/backtest/options") {
        return { ok: true, status: 200, json: async () => OPTIONS };
      }
      return {
        ok: false, status: 409, statusText: "",
        json: async () => ({
          error: { kind: "refusal", message: "Check the MT5 bridge is connected.", ref: null },
        }),
      };
    });
    render(<BacktestPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Scale Out" }));
    await userEvent.click(screen.getByRole("button", { name: /Run backtest/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Check the MT5 bridge is connected.",
    );
  });
});
