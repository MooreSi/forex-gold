import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CrossAssetSection } from "../internal/CrossAssetSection";

/**
 * Other markets against gold. docs/todo/reversal-engine/230.
 *
 * The shape `panel_data.cross_asset_report` returns: `peers`, `coverage`,
 * `daily_corr` (one row per UTC day, a key per peer, null when unmeasured)
 * and `fits` (one row per meta-labeller refit, oldest first).
 */
const PEERS = ["XAGUSD", "USDX"];

const REPORT = {
  peers: PEERS,
  coverage: { measured: 4210, missing: 1360 },
  daily_corr: [
    { day: "2026-09-21", n: 40, XAGUSD: 0.82, USDX: -0.41 },
    { day: "2026-09-22", n: 55, XAGUSD: 0.76, USDX: null },
  ],
  fits: [
    { ts: 1790000000, n: 3900, auc_base: 0.551, auc_xasset: 0.566, installed: "base",
      per_peer: { XAGUSD: { auc_z60: 0.531, n: 3900 }, USDX: { auc_z60: 0.497, n: 3800 } } },
    { ts: 1790086400, n: 4210, auc_base: 0.553, auc_xasset: 0.571, installed: "xasset",
      per_peer: { XAGUSD: { auc_z60: 0.538, n: 4210 }, USDX: { auc_z60: null, n: 0 } } },
  ],
};

describe("CrossAssetSection", () => {
  it("says how much of the history has been measured", () => {
    render(<CrossAssetSection data={REPORT} />);
    expect(screen.getByTestId("xasset-coverage")).toHaveTextContent("4210");
    expect(screen.getByTestId("xasset-coverage")).toHaveTextContent("1360");
  });

  it("draws one correlation line per peer, with a legend", () => {
    render(<CrossAssetSection data={REPORT} />);
    for (const p of PEERS) {
      expect(screen.getByTestId(`xasset-corr-line-${p}`)).toBeInTheDocument();
      expect(screen.getByTestId(`xasset-legend-${p}`)).toBeInTheDocument();
    }
  });

  it("draws the with and without lines across fits", () => {
    render(<CrossAssetSection data={REPORT} />);
    expect(screen.getByTestId("xasset-auc-base")).toBeInTheDocument();
    expect(screen.getByTestId("xasset-auc-xasset")).toBeInTheDocument();
  });

  it("states the latest fit's comparison in words", () => {
    render(<CrossAssetSection data={REPORT} />);
    const latest = screen.getByTestId("xasset-latest");
    expect(latest).toHaveTextContent("0.553");
    expect(latest).toHaveTextContent("0.571");
    expect(latest).toHaveTextContent("+0.018");
  });

  it("lists each peer's own predictive score from the latest fit", () => {
    render(<CrossAssetSection data={REPORT} />);
    expect(screen.getByTestId("xasset-peer-XAGUSD")).toHaveTextContent("0.538");
    // Never measured is a dash, not 0.000.
    expect(screen.getByTestId("xasset-peer-USDX")).toHaveTextContent("—");
  });

  it("a gap in a peer's data breaks its line rather than drawing it at zero", () => {
    render(<CrossAssetSection data={REPORT} />);
    const usdx = screen.getByTestId("xasset-corr-line-USDX");
    // one measured day: a single point, so no segment is drawn to a zero
    expect(usdx.getAttribute("d") ?? "").not.toMatch(/L/);
  });

  it("before anything is measured it says so instead of drawing empty axes", () => {
    render(<CrossAssetSection data={{ peers: PEERS, coverage: { measured: 0, missing: 5000 },
      daily_corr: [], fits: [] }} />);
    expect(screen.queryByTestId("xasset-corr-chart")).toBeNull();
    expect(screen.getByText(/being measured/i)).toBeInTheDocument();
  });

  it("an empty payload is an empty state", () => {
    render(<CrossAssetSection data={{}} />);
    expect(screen.queryByTestId("xasset-corr-chart")).toBeNull();
  });
});
