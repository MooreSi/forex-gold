import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActiveTradesSection } from "../internal/ActiveTradesSection";
import type { Trade } from "@/api/types";

/**
 * The Positions table, and the two things it was getting wrong on 2026-09-21.
 *
 * * "current positions do not show the current p&l and this should
 *   automatically update" — the column read `mt5_profit`, which is only
 *   written when a trade CLOSES, so every open row showed an em dash. The
 *   running figure now comes from the broker's live positions payload and
 *   arrives with the table's own poll.
 * * "it is showing one position when on mt5 there are two" — the table is the
 *   app's own record, and a position opened by hand in MetaTrader has no
 *   record. It is shown now, flagged, and cannot be closed from here.
 *
 * The merge itself is the backend's and is tested there
 * (tests/positions/test_open_positions_view.py). What is tested here is what
 * the screen does with the answer.
 */
function trade(over: Partial<Trade> = {}): Trade {
  return {
    id: "t-1", direction: "BUY", lots: 0.1, entry: 2650, sl: 2640, tp: 2670,
    pnl: 12.5, mt5_ticket: 111, strategy_label: "Scalp", source_label: "GoldSignals",
    untracked: false,
    ...over,
  };
}

const render_ = (trades: Trade[], disabledReason: string | null = null) =>
  render(<ActiveTradesSection trades={trades} disabledReason={disabledReason}
                              onChanged={() => {}} />);

describe("the running P&L", () => {
  it("shows the broker's number with its sign", () => {
    render_([trade({ pnl: 12.5 })]);

    expect(screen.getByTestId("position-pnl")).toHaveTextContent("+$12.50");
  });

  it("shows a loss as a loss", () => {
    render_([trade({ pnl: -12.5 })]);

    const cell = screen.getByTestId("position-pnl");
    expect(cell).toHaveTextContent("-$12.50");
    expect(cell.className).toContain("loss");
  });

  it("shows an em dash, never a zero, when the broker did not report one", () => {
    // $0.00 reads as break-even, which is a number an operator would act on.
    render_([trade({ pnl: undefined })]);

    expect(screen.getByTestId("position-pnl")).toHaveTextContent("—");
  });
});

describe("a position the app has no record of", () => {
  it("is shown rather than left out", () => {
    // "MT5 shows two and this shows one" is the table being confidently wrong.
    render_([trade(), trade({ id: undefined, mt5_ticket: 222, untracked: true })]);

    expect(screen.getAllByTestId(/position-row/)).toHaveLength(2);
  });

  it("is marked as untracked", () => {
    render_([trade({ id: undefined, mt5_ticket: 222, untracked: true,
                     source_label: "Opened in MT5 (not tracked)" })]);

    expect(screen.getByTestId("position-row-untracked"))
      .toHaveTextContent("Opened in MT5 (not tracked)");
  });

  it("says how many there are, once, at the top", () => {
    render_([trade(), trade({ id: undefined, mt5_ticket: 222, untracked: true })]);

    expect(screen.getByTestId("untracked-summary")).toHaveTextContent("1 position is open");
  });

  it("says nothing at the top when every position is accounted for", () => {
    render_([trade()]);

    expect(screen.queryByTestId("untracked-summary")).not.toBeInTheDocument();
  });

  it("cannot be closed from here, and says why", () => {
    // There is no trade_id to close against and no record to update
    // afterwards. The backend sends no id either; this is the visible half.
    render_([trade({ id: undefined, mt5_ticket: 222, untracked: true })]);

    const button = within(screen.getByTestId("position-row-untracked"))
      .getByRole("button", { name: "Close" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("MetaTrader 5"));
  });

  it("does not disable Close on the tracked rows beside it", () => {
    render_([trade(), trade({ id: undefined, mt5_ticket: 222, untracked: true })]);

    expect(within(screen.getByTestId("position-row")).getByRole("button", { name: "Close" }))
      .toBeEnabled();
  });
});

describe("matching a row against MetaTrader", () => {
  it("shows the broker's ticket", () => {
    // The column that lets an operator check "one here, two there" themselves.
    render_([trade({ mt5_ticket: 2050682687 })]);

    expect(screen.getByTestId("position-row")).toHaveTextContent("2050682687");
  });

  it("shows a dash for a position with no ticket yet", () => {
    render_([trade({ mt5_ticket: undefined })]);

    expect(within(screen.getByTestId("position-row")).getAllByText("—").length)
      .toBeGreaterThan(0);
  });

  it("shows the take-profit", () => {
    render_([trade({ tp: 2670 })]);

    expect(screen.getByTestId("position-row")).toHaveTextContent("2670");
  });
});

describe("when trading is halted", () => {
  it("still lists the positions, with Close explaining itself", () => {
    render_([trade()], "The circuit breaker has tripped.");

    const button = within(screen.getByTestId("position-row"))
      .getByRole("button", { name: "Close" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "The circuit breaker has tripped.");
  });
});

describe("a position the remote node opened", () => {
  // Both nodes share one MT5 account. Everything the VPS opens has no record
  // on this machine, and until 2026-09-23 each one was called "not tracked"
  // and the operator was told to close it in MetaTrader.
  const remote = (over: Partial<Trade> = {}) => trade({
    id: undefined, mt5_ticket: 222, untracked: true, remote: true,
    source_label: "Remote node: GoldSignals", ...over,
  });

  it("gets its own row marker, not the untracked one", () => {
    render_([remote()]);

    expect(screen.getByTestId("position-row-remote"))
      .toHaveTextContent("Remote node: GoldSignals");
    expect(screen.queryByTestId("position-row-untracked")).not.toBeInTheDocument();
  });

  it("is not counted as a position this app has no record of", () => {
    render_([remote()]);

    expect(screen.queryByTestId("untracked-summary")).not.toBeInTheDocument();
  });

  it("is counted apart from a genuinely untracked one beside it", () => {
    render_([remote(), trade({ id: undefined, mt5_ticket: 333, untracked: true })]);

    expect(screen.getByTestId("untracked-summary")).toHaveTextContent("1 position is open");
  });

  it("cannot be closed from here, and says the remote node manages it", () => {
    render_([remote()]);

    const button = within(screen.getByTestId("position-row-remote"))
      .getByRole("button", { name: "Close" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("remote node"));
  });
});
