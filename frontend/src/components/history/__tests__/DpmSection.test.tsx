import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DpmSection } from "../internal/DpmSection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * DPM: the sixth NiceGUI Analysis sub-tab, and the last one the port missed.
 *
 * `/api/ai/dpm` has served all three tables since the port; nothing rendered
 * them. The three answer different questions and are kept apart on purpose —
 * whether the calibration has been run and on how much evidence, what the
 * multipliers currently are, and what each managed trade actually did under
 * the parameters in force when it opened.
 */
let body: Record<string, unknown>;

beforeEach(() => {
  resetPolls();
  body = {
    runs: [{ calibrated_at: 1757955600, buckets: 6, total_samples: 120,
             avg_win_rate: 58.4, avg_r: 0.42, avg_pf: 1.6 }],
    calibration: [
      { session: "London", momentum_bucket: "strong", be_multiplier: 1.25,
        trail_multiplier: 2.5, tp1_partial_pct: 50, sample_size: 40,
        win_rate: 62.5, avg_r_multiple: 0.55 },
      { session: "Asian", momentum_bucket: "weak", be_multiplier: 0.9,
        trail_multiplier: 1.5, tp1_partial_pct: 30, sample_size: 4,
        win_rate: 25, avg_r_multiple: null },
    ],
    performance: [
      { trade_id: "t1", direction: "BUY", exit_type: "trail", final_pnl: 88.4,
        r_multiple: 1.8, hold_minutes: 95, session_at_entry: "London",
        momentum_label: "strong", be_multiplier_used: 1.25,
        trail_multiplier_used: 2.5, close_time: 1757955600 },
    ],
  };
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200, json: async () => body,
  })));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("the calibration runs", () => {
  it("says how much evidence a run was built on", async () => {
    // A calibration on forty trades is a different object from one on four.
    render(<DpmSection />);

    expect(await screen.findByTestId("dpm-runs")).toHaveTextContent("120");
  });

  it("says plainly when DPM has never been calibrated", async () => {
    body = { ...body, runs: [] };
    render(<DpmSection />);

    expect(await screen.findByText(/never been calibrated/)).toBeInTheDocument();
  });
});

describe("the multipliers in force", () => {
  it("shows the breakeven and trail for a session and momentum", async () => {
    render(<DpmSection />);
    const row = await screen.findByTestId("dpm-bucket-London-strong");

    expect(row).toHaveTextContent("1.25x");
    expect(row).toHaveTextContent("2.50x");
  });

  it("flags a bucket calibrated on too little evidence", async () => {
    // A multiplier derived from four trades is in force exactly as much as
    // one derived from four hundred, and the screen has to say which is which.
    render(<DpmSection />);
    const thin = await screen.findByTestId("dpm-bucket-Asian-weak");

    expect(within(thin).getByText("4").className).toContain("text-warning");
  });

  it("leaves an unmeasured average blank rather than showing 0.00", async () => {
    // Zero R and "not measured" are different statements.
    render(<DpmSection />);
    const thin = await screen.findByTestId("dpm-bucket-Asian-weak");

    expect(thin).not.toHaveTextContent("0.00");
  });

  it("says the defaults are in use when nothing is calibrated", async () => {
    body = { ...body, calibration: [] };
    render(<DpmSection />);

    expect(await screen.findByText(/built-in defaults/)).toBeInTheDocument();
  });
});

describe("the managed trades", () => {
  it("shows what the trade did", async () => {
    render(<DpmSection />);
    const row = await screen.findByTestId("dpm-trade-t1");

    expect(row).toHaveTextContent("trail");
    expect(row).toHaveTextContent("$88.40");
    expect(row).toHaveTextContent("1.80");
  });

  it("shows the parameters that were in effect when it opened", async () => {
    // Not the ones in force now. A trade managed under an old calibration is
    // the evidence for changing it.
    render(<DpmSection />);

    expect(await screen.findByTestId("dpm-trade-t1")).toHaveTextContent("1.25x");
  });

  it("says when DPM has managed nothing yet", async () => {
    body = { ...body, performance: [] };
    render(<DpmSection />);

    expect(await screen.findByText(/has not managed a closed trade/)).toBeInTheDocument();
  });
});

describe("what it survives", () => {
  it("renders when every table is empty, as on a fresh install", async () => {
    body = { runs: [], calibration: [], performance: [] };
    render(<DpmSection />);

    expect(await screen.findByText(/never been calibrated/)).toBeInTheDocument();
  });

  it("renders when the payload is not the shape it expects", async () => {
    body = { runs: "no", calibration: null, performance: undefined as never };
    render(<DpmSection />);

    expect(await screen.findByText(/never been calibrated/)).toBeInTheDocument();
  });
});
