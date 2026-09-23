import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { OpenPositionsCard } from "../internal/OpenPositionsCard";
import type { Trade } from "@/api/types";

const base: Trade = {
  direction: "SELL", entry: 2700, lots: 0.2, pnl: 7, mt5_ticket: 222,
  untracked: true,
} as Trade;

describe("the open-positions card", () => {
  it("names a position the remote node opened as the remote node's", () => {
    // Both nodes share one MT5 account. "untracked" on a VPS trade tells the
    // operator a healthy pair has lost track of its own position.
    render(<OpenPositionsCard trades={[{ ...base, remote: true }]} />);

    expect(screen.getByText("remote node")).toBeInTheDocument();
    expect(screen.queryByText("untracked")).not.toBeInTheDocument();
  });

  it("still calls a stranger's position untracked", () => {
    render(<OpenPositionsCard trades={[{ ...base, remote: false }]} />);

    expect(screen.getByText("untracked")).toBeInTheDocument();
  });
});
