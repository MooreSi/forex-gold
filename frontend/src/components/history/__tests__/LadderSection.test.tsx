import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LadderSection } from "../internal/LadderSection";

/**
 * Ladder reach, as the backend actually sends it (owner, 2026-10-21: "this
 * doesn't look to be working"). The table read a `reach` field nothing ever
 * sent, so every strategy showed a dash. The real fields are the rung buckets.
 */
const row = {
  n: 20, win_rate: 55.0, net_pnl: 120.5, no_tp: 6, tp1_2: 9, tp3_4: 4,
  tp5_plus: 1, stopped_after_tp: 7, top_rung: 6, avg_rung: 1.65,
};

describe("LadderSection", () => {
  it("shows the average rung reached and where the trades finished", () => {
    render(<LadderSection ladder={{ "template:GD VIP - Single": row }} />);
    const tr = screen.getByText("GD VIP - Single").closest("tr")!;
    const cells = within(tr).getAllByRole("cell").map((c) => c.textContent);

    expect(cells).toContain("TP 1.65");
    expect(cells).toContain("20");
    expect(cells).toContain("6");   // no TP
    expect(cells).toContain("9");   // TP1-2
    expect(cells).toContain("7");   // stopped after a rung
    expect(cells).toContain("TP6"); // deepest rung
  });

  it("lists the strategy with the most trades first", () => {
    render(<LadderSection ladder={{ small: { ...row, n: 5 }, big: { ...row, n: 40 } }} />);
    const names = screen.getAllByRole("row").slice(1).map((r) => r.querySelector("td")?.textContent);

    expect(names).toEqual(["big", "small"]);
  });

  it("says so when no strategy has enough trades", () => {
    render(<LadderSection ladder={{}} />);
    expect(screen.getByText(/Not enough closed trades/)).toBeInTheDocument();
  });
});
