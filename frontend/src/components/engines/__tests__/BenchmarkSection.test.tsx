import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BenchmarkSection } from "../internal/BenchmarkSection";

/**
 * Does the Reversal Engine beat chance? docs/todo/reversal-engine/220.
 *
 * The shape `chance_benchmark.benchmark` really returns, checked against the
 * live payload on 2026-09-23 (5,555 closed rows). A test that invents its own
 * keys passes against a component reading the same invented keys -- the way
 * the pro-model section went a year reporting nothing.
 */
const g = (name: string, n: number, exp: number, act: number, z: number,
  net: number, verdict: string) => ({
  name, n, expected_win_pct: exp, actual_win_pct: act,
  excess_pct: +(act - exp).toFixed(2), z, mean_net: net, verdict,
});

const WINDOW_ALL = {
  days: null,
  overall: g("", 4681, 75.5, 74.43, -1.73, -4.7, "no evidence"),
  groups: {
    level_type: [g("round_5", 1400, 76.6, 72.05, -4.37, -4.8, "worse than chance")],
    session: [g("asian", 1600, 75.36, 71.86, -3.6, -5.09, "worse than chance"),
              g("london", 800, 75.59, 73.75, -1.31, -3.47, "no evidence")],
    bias: [g("against", 1500, 78.91, 76.53, -2.44, -3.73, "no evidence")],
    hour: [g("14", 90, 75, 80, 1.1, 2.0, "too few")],
  },
  executed: { ...g("", 874, 75.2, 59.27, -11.08, -3.38, "worse than chance"),
              geometry: "engine" },
  ml_auc: { n: 5541, auc: 0.478 },
};

const WINDOW_RECENT = {
  ...WINDOW_ALL,
  days: 14,
  overall: g("", 1247, 75.4, 75.14, -0.22, -4.51, "no evidence"),
  ml_auc: { n: 1378, auc: 0.498 },
};

const REPORT = { all: WINDOW_ALL, recent: WINDOW_RECENT, z_bar: 3, min_n: 100 };

describe("BenchmarkSection", () => {
  it("states the overall verdict with the chance and actual win rates", () => {
    render(<BenchmarkSection benchmark={REPORT} />);
    const overall = screen.getByTestId("benchmark-overall");
    expect(overall).toHaveTextContent("75.5%");
    expect(overall).toHaveTextContent("74.4%");
    expect(overall).toHaveTextContent("no evidence");
  });

  it("switches to the last 14 days", () => {
    render(<BenchmarkSection benchmark={REPORT} />);
    fireEvent.click(screen.getByRole("button", { name: "Last 14 days" }));
    expect(screen.getByTestId("benchmark-overall")).toHaveTextContent("75.1%");
    expect(screen.getByTestId("benchmark-ml")).toHaveTextContent("0.498");
  });

  it("shows the ML gate's own ranking record", () => {
    render(<BenchmarkSection benchmark={REPORT} />);
    expect(screen.getByTestId("benchmark-ml")).toHaveTextContent("0.478");
    expect(screen.getByTestId("benchmark-ml")).toHaveTextContent("5541");
  });

  it("renders one row per group with its verdict and z", () => {
    render(<BenchmarkSection benchmark={REPORT} />);
    fireEvent.click(screen.getByRole("button", { name: "Session" }));
    const row = screen.getByTestId("benchmark-row-asian");
    expect(row).toHaveTextContent("worse than chance");
    expect(row).toHaveTextContent("-3.60");
    expect(screen.getByTestId("benchmark-row-london")).toHaveTextContent("no evidence");
  });

  it("flags executed trades as scored against the wrong geometry", () => {
    render(<BenchmarkSection benchmark={REPORT} />);
    const ex = screen.getByTestId("benchmark-executed");
    expect(ex).toHaveTextContent("874");
    expect(ex).toHaveTextContent(/template/i);
    // Never presented as a verdict: the chance rate it is compared to is wrong.
    expect(within(ex).queryByText("worse than chance")).toBeNull();
  });

  it("an empty payload is an empty state, not a table of zeros", () => {
    render(<BenchmarkSection benchmark={{}} />);
    expect(screen.queryByTestId("benchmark-overall")).toBeNull();
    expect(screen.getByText(/No closed signals/i)).toBeInTheDocument();
  });

  it("a missing AUC is a dash, not 0.000", () => {
    const noAuc = { ...REPORT, all: { ...WINDOW_ALL, ml_auc: { n: 0, auc: null } } };
    render(<BenchmarkSection benchmark={noAuc} />);
    expect(screen.getByTestId("benchmark-ml")).not.toHaveTextContent("0.000");
    expect(screen.getByTestId("benchmark-ml")).toHaveTextContent("—");
  });
});
