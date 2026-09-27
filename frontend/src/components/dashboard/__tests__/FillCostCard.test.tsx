import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FillCostView, type FillCostReport } from "../internal/FillCostCard";

const base: FillCostReport = {
  days: 14, n: 668, unmeasured: 2, median_cost_pts: 0.93, p75_cost_pts: 1.705,
  p90_cost_pts: 2.23, median_slippage_pts: 0.71, adverse_share: 0.66,
  favourable_share: 0.2, median_spread_pts: 0.215, median_cost_r: 0.206,
  by_strategy: [{ strategy: "template:30 TP1 SL50 and Trail", n: 537, median_cost_pts: 1.05,
    p75_cost_pts: null, p90_cost_pts: null, median_slippage_pts: null, adverse_share: 0.7,
    favourable_share: null, median_spread_pts: null, median_cost_r: null }],
};

describe("FillCostView", () => {
  it("shows the measured figures as given", () => {
    render(<FillCostView report={base} />);
    expect(screen.getByText("0.93 pts")).toBeInTheDocument();
    expect(screen.getByText("66%")).toBeInTheDocument();
    expect(screen.getByTestId("fill-cost-summary"))
      .toHaveTextContent("p90 round trip 2.23 pts · median 21% of the stop · 668 fills, 2 not measurable");
  });

  it("splits by strategy without the template prefix", () => {
    render(<FillCostView report={base} />);
    expect(screen.getByTestId("fill-cost-template:30 TP1 SL50 and Trail"))
      .toHaveTextContent("30 TP1 SL50 and Trail5371.05 pts70%");
  });

  it("an empty window says so rather than showing zeros", () => {
    render(<FillCostView report={{ ...base, n: 0, median_cost_pts: null, by_strategy: [] }} />);
    expect(screen.getByText("No measured fills in the last 14 days.")).toBeInTheDocument();
    expect(screen.queryByText("0.00 pts")).toBeNull();
  });
});
