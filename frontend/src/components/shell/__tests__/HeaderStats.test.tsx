import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HeaderStats } from "../HeaderStats";

/**
 * The numbers along the top bar, restored from the NiceGUI header.
 *
 * Six figures in three groups: the price and its spread, the account's cash,
 * and equity against what was actually paid in. Every one of them is a figure
 * an operator glances at rather than reads, so the tests are about the three
 * ways a glance can be misled: a missing number rendered as zero, a loss
 * rendered without its sign, and a stale price rendered as a live one.
 */

const TICK = { bid: 4378.31, ask: 4378.6, mid: 4378.45, spread: 0.29,
               spread_points: 29, timestamp: 1757955600, source: "mt5" };
const ACCOUNT = { balance: 480.91, equity: 480.91, margin_free: 481.0,
                  login: 900123, is_demo: true };

describe("HeaderStats", () => {
  it("shows the bid and the ask as money", async () => {
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={null} stale={false} />);

    expect(screen.getByTestId("stat-bid")).toHaveTextContent("$4,378.31");
    expect(screen.getByTestId("stat-ask")).toHaveTextContent("$4,378.60");
  });

  it("shows the spread in whole points", async () => {
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={null} stale={false} />);

    expect(screen.getByTestId("stat-spread")).toHaveTextContent("spr:29pt");
  });

  it("shows the balance in full and the free margin rounded", async () => {
    // Free margin is a headroom figure, not an accounting one. Pennies on it
    // are noise next to the balance it sits under.
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={null} stale={false} />);

    expect(screen.getByTestId("stat-balance")).toHaveTextContent("$480.91");
    // Exact, not a substring: "free:$481.00" contains "free:$481", so a
    // substring assertion here cannot tell rounded from unrounded.
    expect(screen.getByTestId("stat-free").textContent).toBe("free:$481");
  });

  it("shows equity with the whole-life P&L under it", async () => {
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={-4827.06} stale={false} />);

    expect(screen.getByTestId("stat-equity")).toHaveTextContent("$480.91");
    expect(screen.getByTestId("stat-lifetime")).toHaveTextContent("P&L:$-4827.06");
  });

  it("signs a whole-life profit so it cannot be read as a loss", async () => {
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={412.19} stale={false} />);

    const pnl = screen.getByTestId("stat-lifetime");
    expect(pnl).toHaveTextContent("P&L:$+412.19");
    expect(pnl.className).toContain("text-profit");
  });

  it("colours a whole-life loss red", async () => {
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={-4827.06} stale={false} />);

    expect(screen.getByTestId("stat-lifetime").className).toContain("text-loss");
  });

  it("says nothing at all when the deposits are not known", async () => {
    // Equity shown as profit is the most flattering possible wrong answer.
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={null} stale={false} />);

    expect(screen.queryByTestId("stat-lifetime")).not.toBeInTheDocument();
  });

  it("shows a dash rather than a zero when there is no tick", async () => {
    render(<HeaderStats tick={null} account={ACCOUNT} lifetimePnl={null} stale={false} />);

    expect(screen.getByTestId("stat-bid")).toHaveTextContent("—");
    expect(screen.getByTestId("stat-bid")).not.toHaveTextContent("$0.00");
  });

  it("shows a dash rather than a zero when there is no account", async () => {
    render(<HeaderStats tick={TICK} account={null} lifetimePnl={null} stale={false} />);

    expect(screen.getByTestId("stat-balance")).toHaveTextContent("—");
    expect(screen.getByTestId("stat-balance")).not.toHaveTextContent("$0.00");
  });

  it("marks a price that has stopped updating", async () => {
    // A number that stopped updating reads as a number that stopped moving.
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={null} stale={true} />);

    expect(screen.getByTestId("stat-stale")).toBeInTheDocument();
  });

  it("does not cry stale on a live price", async () => {
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={null} stale={false} />);

    expect(screen.queryByTestId("stat-stale")).not.toBeInTheDocument();
  });

  it("labels every figure", async () => {
    render(<HeaderStats tick={TICK} account={ACCOUNT} lifetimePnl={null} stale={false} />);

    for (const label of ["BID", "ASK", "MT5 BAL", "EQUITY"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });
});
