/**
 * The open positions beside the chart.
 *
 * The table exists to be READ. It was given a draggable third of the width on
 * 2026-09-21 because seven columns in a fixed 20rem wrapped every one of them
 * — "so the detail within the open positions can be viewed easily" — and it
 * then spent the width badly: no gutter between the columns at all, so on a
 * laptop the row rendered as `BUY0.104320.444315.424324.42`, four numbers with
 * nothing between them. Wrapping was traded for running together.
 *
 * jsdom does no layout, so what these last two pin is the contract that makes
 * the layout possible, not the pixels: every cell keeps a gutter, no cell
 * wraps mid-number, and a table too wide for its pane scrolls rather than
 * crushing.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ChartTradesSection } from "../internal/ChartTradesSection";
import type { Trade } from "@/api/types";

const TRADE = {
  id: 1, direction: "BUY", lots: 0.1, entry: 4320.44, sl: 4315.42,
  tp: 4324.42, tg_source: "Gold Diggers VIP", mt5_ticket: 2066935348,
} as unknown as Trade;

describe("the open positions table", () => {
  it("says so when there are none, rather than showing an empty table", () => {
    render(<ChartTradesSection trades={[]} />);

    expect(screen.getByText(/No open positions/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("fills every column from the row", () => {
    // Each of these read as an em dash until 2026-09-21, because the component
    // read short names the payload did not carry.
    render(<ChartTradesSection trades={[TRADE]} />);

    const row = screen.getAllByRole("row")[1]!;
    for (const text of ["BUY", "0.10", "4320.44", "4315.42", "4324.42",
                        "Gold Diggers VIP", "2066935348"]) {
      expect(within(row).getByText(text)).toBeInTheDocument();
    }
  });

  it("keeps a gutter between the columns", () => {
    render(<ChartTradesSection trades={[TRADE]} />);

    const cells = [...screen.getAllByRole("row")[1]!.querySelectorAll("td")];
    expect(cells).toHaveLength(7);
    // The last column needs no right gutter; every one before it does, or its
    // value touches the next.
    for (const cell of cells.slice(0, -1)) {
      expect(cell.className).toMatch(/\bpr-\d/);
    }
  });

  it("scrolls sideways instead of crushing when the pane is narrow", () => {
    render(<ChartTradesSection trades={[TRADE]} />);

    const table = screen.getByRole("table");
    expect(table.className).toContain("whitespace-nowrap");
    expect(table.parentElement?.className).toContain("overflow-x-auto");
  });
});
