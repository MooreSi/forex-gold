/**
 * The proposed trade, and the size that would be placed.
 *
 * The panel's own tests only ever render a BUY. Every direction-sensitive
 * label here has to be checked the other way round too, because a short that
 * renders with a long's words is a card whose numbers all still agree — the
 * levels are right, the arrow is wrong, and nothing on screen contradicts
 * anything else.
 *
 * The other thing pinned here is the sizing arithmetic. The backend sends a
 * PER-LOT cash figure and this is where it gets multiplied, so a mistake is a
 * dollar amount the operator reads before pressing Execute.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { LotSizeSection } from "../internal/LotSizeSection";
import { SetupSummarySection } from "../internal/SetupSummarySection";
import type { SetForgetCandidate, SetForgetEvidence } from "@/api/types";

const EVIDENCE: SetForgetEvidence = {
  price: 2000, weekly_bias: "bullish", daily_bias: "bullish",
  entry_bias: "bullish", entry_timeframe: "4H", zones: [],
  atr: 6, ema_fast: 1995, ema_slow: 1960, rsi: 52, fib: 0.5,
  fib_levels: [], impulse: null, confirmation: null,
};

const LONG: SetForgetCandidate = {
  direction: "BUY", entry: 1985, stop_loss: 1972, take_profit: 2040,
  order_type: "limit", risk: 13, reward: 55, rr: 4.23,
  zone: { kind: "demand", low: 1975, high: 1985, ts: 1, touches: 3 },
};

const SHORT: SetForgetCandidate = {
  ...LONG,
  direction: "SELL", entry: 2015, stop_loss: 2028, take_profit: 1960,
  zone: { kind: "supply", low: 2015, high: 2025, ts: 1, touches: 3 },
};

function summary(over: Partial<Parameters<typeof SetupSummarySection>[0]> = {}) {
  return render(
    <SetupSummarySection
      candidate={LONG}
      evidence={EVIDENCE}
      minRr={2}
      riskMoney={260}
      rewardMoney={1100}
      invalidations={[]}
      {...over}
    />,
  );
}

describe("which trade it is", () => {
  it("labels a long", () => {
    summary();

    expect(screen.getByText("BUY XAUUSD")).toBeInTheDocument();
  });

  it("labels a short, with the short's own bearish colour", () => {
    summary({ candidate: SHORT, evidence: { ...EVIDENCE, weekly_bias: "bearish" } });

    const badge = screen.getByText("SELL XAUUSD");
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain("text-loss");
  });

  it("says a resting order waits, and names the price it waits at", () => {
    summary();

    expect(screen.getByText("Resting limit order")).toBeInTheDocument();
    expect(screen.getByText(/The order waits at 1985\.00/)).toBeInTheDocument();
  });

  it("says a market order fills now", () => {
    summary({ candidate: { ...LONG, order_type: "market" } });

    expect(screen.getByText("Market order")).toBeInTheDocument();
    expect(screen.getByText(/fills now at the market/)).toBeInTheDocument();
  });
});

describe("the ratio", () => {
  it("shows a healthy one plainly", () => {
    summary();

    const chip = screen.getByText("1:4.23 reward-to-risk");
    expect(chip.className).not.toContain("text-loss");
  });

  it("colours a thin one as a problem", () => {
    /* 1:1.50 is a real setup a model will happily propose. It renders exactly
       as tidily as a 1:4 — the colour is the only thing that says otherwise
       before the refusal below it is read. */
    summary({ candidate: { ...LONG, rr: 1.5 } });

    expect(screen.getByText("1:1.50 reward-to-risk").className)
      .toContain("text-loss");
  });

  it("shows no chip at all rather than a blank one when there is no ratio", () => {
    summary({ candidate: { ...LONG, rr: null } });

    expect(screen.queryByText(/reward-to-risk/)).not.toBeInTheDocument();
  });
});

describe("the refusals", () => {
  it("renders every reason, loudly, as an alert", () => {
    summary({
      invalidations: ["Reward-to-risk is 1:0.60.", "A BUY's stop must be below."],
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("1:0.60");
    expect(alert).toHaveTextContent("stop must be below");
  });

  it("shows nothing when the setup is sound", () => {
    summary();

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("the cash beside each level", () => {
  it("prints what a stop-out costs and what the target pays", () => {
    summary();

    expect(screen.getByText(/13\.00 pts · \$260\.00/)).toBeInTheDocument();
    expect(screen.getByText(/55\.00 pts · \$1,100\.00/)).toBeInTheDocument();
  });

  it("omits the cash until a size has been chosen", () => {
    /* $0.00 is a real answer meaning "this trade wins nothing", and it is not
       the same as not having decided how much to trade. */
    summary({ riskMoney: null, rewardMoney: null });

    expect(screen.getByText(/13\.00 pts/)).toBeInTheDocument();
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
  });
});

describe("the lot selector", () => {
  function lots(over: Partial<Parameters<typeof LotSizeSection>[0]> = {}) {
    const onPick = vi.fn();
    render(
      <LotSizeSection
        lots={0.1}
        onPick={onPick}
        suggested={0.04}
        riskPerLot={1300}
        rewardPerLot={5500}
        riskPct={1}
        balance={5000}
        {...over}
      />,
    );
    return onPick;
  }

  it("multiplies the per-lot figures by the chosen size", () => {
    lots();

    expect(screen.getByText("$130.00")).toBeInTheDocument();
    expect(screen.getByText("$550.00")).toBeInTheDocument();
  });

  it("says what fraction of the balance is at risk", () => {
    lots();

    expect(screen.getByText("2.6% of balance")).toBeInTheDocument();
  });

  it("offers the risk-based size and passes it on when pressed", async () => {
    const onPick = lots();

    await userEvent.click(screen.getByRole("button", { name: /Size from risk/ }));

    expect(onPick).toHaveBeenCalledWith(0.04);
  });

  it("refuses to suggest a size when the balance could not be read, and says why",
     async () => {
    /* A lot computed against an invented balance looks exactly like a real
       one, and it would go to a broker. */
    lots({ suggested: null, balance: null });

    const button = screen.getByRole("button", { name: /Size from risk/ });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("balance"));
  });

  it("marks the chosen step as pressed so the state is visible", () => {
    lots({ lots: 0.5 });

    expect(screen.getByRole("button", { name: "0.50" }))
      .toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "0.10" }))
      .toHaveAttribute("aria-pressed", "false");
  });

  it("says no size has been chosen rather than showing zero", () => {
    lots({ lots: null });

    expect(screen.getByText(/No size chosen yet/)).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });
});
