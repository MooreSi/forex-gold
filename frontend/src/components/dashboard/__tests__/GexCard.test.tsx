import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GexView, type GexReport } from "../internal/GexCard";

const base: GexReport = {
  n_snapshots: 5, target_snapshots: 63, age_days: 0.1, stale: false,
  regime: "negative", spot_vs_flip: "below",
  snapshot: {
    asof_date: "2026-09-29", taken_at: 1_790_000_000, underlying: "GLD",
    spot: 393.41, xau_spot: 4250, ratio: 10.8, total_gex: -1.5e9,
    flip_level: 393.91, call_wall: 400, put_wall: 380,
    xau_flip_level: 4254.2, xau_call_wall: 4320, xau_put_wall: 4104,
    n_rows: 1764, expiries: ["2026-10-02", "2026-11-20"], source: "yfinance",
    assumptions: "naive GEX: dealers long calls, short puts",
  },
};

describe("GexView", () => {
  it("shows the levels in XAUUSD with GLD beside them", () => {
    render(<GexView report={base} />);
    expect(screen.getByTestId("gex-flip")).toHaveTextContent("4254.20GLD 393.91");
    expect(screen.getByTestId("gex-call-wall")).toHaveTextContent("4320.00GLD 400.00");
    expect(screen.getByTestId("gex-put-wall")).toHaveTextContent("4104.00GLD 380.00");
  });

  it("names the regime from the stored sign, with the size per 1% move", () => {
    render(<GexView report={base} />);
    expect(screen.getByTestId("gex-regime"))
      .toHaveTextContent("Negative gamma · -$1.50B per 1% · spot below the flip");
  });

  it("shows how far the history has got toward the study", () => {
    render(<GexView report={base} />);
    expect(screen.getByTestId("gex-history")).toHaveTextContent("5 of 63 snapshots");
  });

  it("an old snapshot says it is old", () => {
    render(<GexView report={{ ...base, stale: true, age_days: 6.2 }} />);
    expect(screen.getByTestId("gex-stale")).toHaveTextContent("6 days old");
  });

  it("no snapshot yet says so rather than showing zeros", () => {
    render(<GexView report={{ ...base, snapshot: null, n_snapshots: 0, regime: null,
      spot_vs_flip: null, stale: null, age_days: null }} />);
    expect(screen.getByText(/No GEX snapshot yet/)).toBeInTheDocument();
    expect(screen.queryByText("0.00")).toBeNull();
  });

  it("a missing level shows a dash, not zero", () => {
    render(<GexView report={{ ...base, regime: null, spot_vs_flip: null,
      snapshot: { ...base.snapshot!, flip_level: null, xau_flip_level: null, total_gex: null } }} />);
    expect(screen.getByTestId("gex-flip")).toHaveTextContent("—");
    expect(screen.getByTestId("gex-regime")).toHaveTextContent("Regime unknown");
  });
});
