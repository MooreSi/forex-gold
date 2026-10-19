import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { SetForgetCandidate, SetForgetEvidence } from "@/api/types";
import { SetupSummarySection } from "../internal/SetupSummarySection";

const candidate: SetForgetCandidate = {
  direction: "BUY", entry: 2004, stop_loss: 1994, take_profit: 2034,
  order_type: "market", risk: 10, reward: 30, rr: 3, stage: "triggered",
  trigger: { kind: "engulfing", ts: 1792404000, level: 1996 },
  distance: 0, distance_days: 0,
};
const evidence: SetForgetEvidence = {
  price: 2004, weekly_bias: "bullish", daily_bias: "bullish", entry_bias: "bullish",
  entry_timeframe: "4H", zones: [], atr: 4, daily_atr: 20,
  ema_fast: null, ema_slow: null, rsi: null, fib: null, fib_levels: [],
  impulse: null, confirmation: null,
};

function show(c: SetForgetCandidate) {
  render(<SetupSummarySection candidate={c} evidence={evidence} minRr={2}
    riskMoney={null} rewardMoney={null} invalidations={[]} />);
}

describe("the measured reason for a setup", () => {
  it("names the closed M30 trigger", () => {
    show(candidate);
    expect(screen.getByText(/30m engulfing confirmed/)).toBeInTheDocument();
  });

  it("does not describe a watched plan as an order already resting", () => {
    show({ ...candidate, stage: "armed", order_type: "limit", trigger: null });
    expect(screen.getByText(/Watching the zone/)).toBeInTheDocument();
    expect(screen.queryByText("Resting limit order")).not.toBeInTheDocument();
  });

  it("shows the actual projection ratio after an accepted AI adjustment", () => {
    show({ ...candidate, target_basis: "risk_multiple", rr: 2,
      take_profit: 2024, reward: 20 });
    expect(screen.getByText(/2.00R projection/)).toBeInTheDocument();
  });
});
