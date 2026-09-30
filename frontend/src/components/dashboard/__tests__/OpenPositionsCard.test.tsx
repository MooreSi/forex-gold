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

  it("names the source of a remote node's position, as it does a local one", () => {
    // Owner, 2026-09-30: with the VPS trading, every row read "remote node"
    // and the channel that opened it was nowhere on the dashboard.
    render(<OpenPositionsCard trades={[
      { ...base, remote: true, tg_source: "Gold Diggers VIP" }]} />);

    expect(screen.getByText("Gold Diggers VIP")).toBeInTheDocument();
    expect(screen.queryByText("remote node")).not.toBeInTheDocument();
  });

  it("still calls a stranger's position untracked", () => {
    render(<OpenPositionsCard trades={[{ ...base, remote: false }]} />);

    expect(screen.getByText("untracked")).toBeInTheDocument();
  });
});
