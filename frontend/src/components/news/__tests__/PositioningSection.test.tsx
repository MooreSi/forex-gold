import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PositioningSection } from "../internal/PositioningSection";
import { resetPolls } from "@/hooks/usePoll";

/** CFTC managed money and GLD holdings (docs/todo/024). Display only. */
const SNAP = {
  cot: { report_date: "2026-10-13", mm_long: 124747, mm_short: 17658, mm_net: 107089,
         net_change: -3898, net_pct_oi: 27.0, open_interest: 396345, cot_index: 45.1,
         weeks: 157, series: [] },
  cot_fetched_at: 1_790_000_000, cot_error: null,
  gld: { date: "2026-10-19", tonnes: 1058.47, change_1d: -0.57, change_5d: 3.06,
         change_20d: 3.06, series: [] },
  gld_fetched_at: 1_790_000_000, gld_error: null,
};

let body: Record<string, unknown>;

beforeEach(() => {
  resetPolls();
  body = SNAP;
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => body })));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("PositioningSection", () => {
  it("shows managed money and the COT index", async () => {
    render(<PositioningSection />);
    const box = await screen.findByTestId("positioning");
    expect(box).toHaveTextContent("107,089");
    expect(box).toHaveTextContent("-3,898");
    expect(box).toHaveTextContent("45");
    expect(box).toHaveTextContent("2026-10-13");
  });

  it("shows GLD tonnes and their change", async () => {
    render(<PositioningSection />);
    const box = await screen.findByTestId("positioning");
    expect(box).toHaveTextContent("1,058.5 t");
    expect(box).toHaveTextContent("+3.06 t");
  });

  it("keeps old numbers on screen with the error beside them", async () => {
    body = { ...SNAP, cot_error: "cftc down" };
    render(<PositioningSection />);
    const box = await screen.findByTestId("positioning");
    expect(box).toHaveTextContent("107,089");
    expect(box).toHaveTextContent("cftc down");
  });

  it("says so when a source has never answered", async () => {
    body = { ...SNAP, gld: null, gld_error: "no rows" };
    render(<PositioningSection />);
    const box = await screen.findByTestId("positioning");
    expect(box).toHaveTextContent("no rows");
  });
});
