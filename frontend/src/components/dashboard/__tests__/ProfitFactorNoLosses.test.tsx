/**
 * A window with winners and no losers reads "No losses", not a red 0.00
 * (owner, 2026-10-16). The backend still sends `profit_factor: 0` there (it
 * is pinned) and adds `no_losses: true`; both cards read the flag. 0.00 stays
 * for a window with no winners, which is the true figure.
 */
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { PerformanceCard } from "../internal/PerformanceCard";
import { PerformanceSection } from "@/components/history/internal/PerformanceSection";
import { DASHBOARD_WINDOWS } from "../hooks/useDashboardWindow";

const ALL_WINS = {
  total_net_pnl: 29.57, closed_trades: 2, win_rate_pct: 100,
  profit_factor: 0, no_losses: true, max_drawdown_pct: 0, balance: 1000,
};
const ALL_LOSSES = { ...ALL_WINS, total_net_pnl: -10, win_rate_pct: 0, no_losses: false };

const card = (performance: Record<string, unknown>) =>
  render(<PerformanceCard performance={performance} range={DASHBOARD_WINDOWS[0]}
                          onWindow={() => {}} />);

it("the dashboard card says No losses for an all-winners window", () => {
  card(ALL_WINS);
  expect(screen.getByText("No losses")).toBeInTheDocument();
  expect(screen.queryByText("0.00")).toBeNull();
});

it("the dashboard card keeps 0.00 when nothing won", () => {
  card(ALL_LOSSES);
  expect(screen.getByText("0.00")).toBeInTheDocument();
  expect(screen.queryByText("No losses")).toBeNull();
});

it("the History card says No losses too", () => {
  render(<PerformanceSection performance={ALL_WINS} />);
  expect(screen.getByText("No losses")).toBeInTheDocument();
  expect(screen.queryByText("0.00")).toBeNull();
});

it("the History card keeps a real figure", () => {
  render(<PerformanceSection performance={{ ...ALL_WINS, profit_factor: 2, no_losses: false }} />);
  expect(screen.getByText("2.00")).toBeInTheDocument();
});
