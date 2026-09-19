import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BreakoutSection } from "../internal/BreakoutSection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The Breakout engine's panel, which was never ported.
 *
 * `panel_data.py` declared seventeen reads and nothing called any of them. The
 * Signal Generator tab showed the Reversal engine's ML gate and virtual trades
 * in detail and said nothing about Breakout beyond a Start/Stop card.
 *
 * The four splits are the reason the panel exists: an aggregate win rate hides
 * a time window that only loses, and this engine's 12:00-15:00 UTC losses hid
 * in its own headline figure for weeks.
 */
let body: Record<string, unknown>;

beforeEach(() => {
  resetPolls();
  body = {
    stats: { total: 12, wins: 7, win_rate: 58.3, net_pnl: 210.4 },
    virtual_balance: 1210.4,
    max_drawdown: 8.2,
    ml: {
      summary: { trained: true, labeled_count: 40, min_needed: 15 },
      metrics: { brier_now: 0.213, mcc_rolling: 0.31 },
      thresholds: { min_train_samples: 15, retrain_every: 5 },
    },
    by_session: [{ session: "London", n: 6, net_pnl: 120 },
                 { session: "NY", n: 6, net_pnl: 90.4 }],
    by_adx: [{ band: "20-25", n: 4, net_pnl: -30 }],
    by_type: [{ type: "range", n: 5, net_pnl: 80 }],
    by_bias: [{ bias: "with", n: 7, net_pnl: 150 }],
  };
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200, json: async () => body,
  })));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("the headline figures", () => {
  it("shows what the engine has actually done", async () => {
    render(<BreakoutSection />);

    expect(await screen.findByTestId("bo-total")).toHaveTextContent("12");
    expect(screen.getByTestId("bo-win-rate")).toHaveTextContent("58.3%");
    expect(screen.getByTestId("bo-pnl")).toHaveTextContent("$210.40");
  });

  it("says how many trades the win rate is over", async () => {
    // 100% over two trades is not a measurement.
    render(<BreakoutSection />);
    await screen.findByTestId("bo-win-rate");

    expect(screen.getByText("of 12")).toBeInTheDocument();
  });

  it("separates the engine's paper balance from the account", async () => {
    render(<BreakoutSection />);

    expect(await screen.findByTestId("bo-balance")).toHaveTextContent("$1,210.40");
    expect(screen.getByText(/not the account/)).toBeInTheDocument();
  });

  it("shows a dash rather than a zero for a figure it does not have", async () => {
    // 0.0% drawdown reads as an engine that never lost. A fresh install is
    // not that.
    body = { ...body, max_drawdown: null };
    render(<BreakoutSection />);

    expect(await screen.findByTestId("bo-drawdown")).toHaveTextContent("—");
  });
});

describe("the classifier", () => {
  it("says whether it is in use", async () => {
    render(<BreakoutSection />);

    expect(await screen.findByTestId("bo-ml-state")).toHaveTextContent("in use");
  });

  it("says how far off an untrained one is", async () => {
    // "40 labelled" means nothing without the number it has to reach.
    body = { ...body, ml: { ...(body.ml as object),
             summary: { trained: false, labeled_count: 9, min_needed: 15 } } };
    render(<BreakoutSection />);

    expect(await screen.findByTestId("bo-ml-state")).toHaveTextContent("not trained");
    expect(screen.getByText(/9 labelled of 15 needed/)).toBeInTheDocument();
  });

  it("shows the calibration scores when it has them", async () => {
    render(<BreakoutSection />);
    await screen.findByTestId("bo-ml-state");

    expect(screen.getByText(/Brier 0.213/)).toBeInTheDocument();
    expect(screen.getByText(/MCC 0.310/)).toBeInTheDocument();
  });

  it("omits a score it does not have rather than showing zero", async () => {
    // A Brier of 0.000 is a perfect model. "Never measured" is not.
    body = { ...body, ml: { ...(body.ml as object), metrics: {} } };
    render(<BreakoutSection />);
    await screen.findByTestId("bo-ml-state");

    expect(screen.queryByText(/Brier/)).not.toBeInTheDocument();
  });
});

describe("the splits", () => {
  it("breaks performance down four ways", async () => {
    render(<BreakoutSection />);

    expect(await screen.findByTestId("bo-by-session")).toBeInTheDocument();
    expect(screen.getByTestId("bo-by-adx")).toBeInTheDocument();
    expect(screen.getByTestId("bo-by-type")).toBeInTheDocument();
    expect(screen.getByTestId("bo-by-bias")).toBeInTheDocument();
  });

  it("names each row by its own key, which differs per split", async () => {
    render(<BreakoutSection />);
    await screen.findByTestId("bo-by-session");

    expect(within(screen.getByTestId("bo-by-session")).getByText("London")).toBeInTheDocument();
    expect(within(screen.getByTestId("bo-by-adx")).getByText("20-25")).toBeInTheDocument();
    expect(within(screen.getByTestId("bo-by-bias")).getByText("with")).toBeInTheDocument();
  });

  it("shows a losing window as losing", async () => {
    // The whole reason the splits exist: an aggregate win rate hides a time
    // window that only loses.
    render(<BreakoutSection />);
    await screen.findByTestId("bo-by-adx");

    expect(within(screen.getByTestId("bo-by-adx")).getByText("-$30.00").className)
      .toContain("text-loss");
  });

  it("says a split with nothing in it is empty rather than drawing a blank table", async () => {
    body = { ...body, by_type: [] };
    render(<BreakoutSection />);
    await screen.findByTestId("bo-by-session");

    expect(screen.getByText("nothing closed yet")).toBeInTheDocument();
  });
});

describe("a fresh install", () => {
  it("renders when the engine has no database at all", async () => {
    body = {
      stats: {}, virtual_balance: null, max_drawdown: null,
      ml: { summary: {}, metrics: {}, thresholds: {} },
      by_session: [], by_adx: [], by_type: [], by_bias: [],
    };
    render(<BreakoutSection />);

    expect(await screen.findByTestId("bo-total")).toHaveTextContent("—");
  });

  it("renders when the payload is not the shape it expects", async () => {
    body = { stats: "no", ml: null, by_session: "nope" };
    render(<BreakoutSection />);

    expect(await screen.findByTestId("bo-ml-state")).toBeInTheDocument();
  });
});
