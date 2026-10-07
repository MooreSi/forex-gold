import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BacktestResults } from "../internal/BacktestResults";
import type { BacktestResult } from "@/api/types";

/**
 * The in-sample / out-of-sample halves (docs/todo/003), shown with their trade
 * counts (handover 036, owner 2026-10-07: keep the 20-trade minimum, show the
 * count beside every figure). The API always sent the halves; the table never
 * drew them, so a split run looked exactly like an unsplit one.
 */
function stats(over: Record<string, unknown> = {}) {
  return {
    strategy: "Test", trades: 50, wins: 30, losses: 20, win_rate: 60,
    total_pnl: 120, total_commission: 0, avg_win: 10, avg_loss: -9,
    profit_factor: 1.5, max_drawdown_pct: 4, sharpe: 1, final_balance: 10120,
    equity_curve: [], unsupported_reason: "", ...over,
  };
}

function result(split: unknown): BacktestResult {
  return {
    results: [stats({ split })], filtered: { valid: 50, total: 50 },
    signals_loaded: 50, candles_loaded: 1000, granularity: "m1",
  } as unknown as BacktestResult;
}

describe("the split halves", () => {
  it("shows each half with its own trade count", () => {
    render(<BacktestResults result={result({
      boundary_ts: 1790000000, requested_frac: 0.5, achieved_frac: 0.5,
      in_sample: stats({ trades: 26, win_rate: 65 }),
      out_of_sample: stats({ trades: 24, win_rate: 54.2 }),
      in_sample_note: "", out_of_sample_note: "",
    })} />);
    expect(screen.getByText(/in-sample/i)).toBeInTheDocument();
    expect(screen.getByText(/out-of-sample/i)).toBeInTheDocument();
    expect(screen.getByText("26")).toBeInTheDocument();
    expect(screen.getByText("24")).toBeInTheDocument();
  });

  it("shows the note, not numbers, for a half under the minimum", () => {
    render(<BacktestResults result={result({
      boundary_ts: 1790000000, requested_frac: 0.5, achieved_frac: 0.5,
      in_sample: stats({ trades: 30 }), out_of_sample: null,
      in_sample_note: "", out_of_sample_note: "6 trades -- too few to measure (min 20; see docs/simon-handover/036)",
    })} />);
    expect(screen.getByText(/6 trades -- too few to measure/)).toBeInTheDocument();
  });

  it("draws no extra rows for an unsplit run", () => {
    render(<BacktestResults result={result(null)} />);
    expect(screen.queryByText(/in-sample/i)).toBeNull();
  });
});
