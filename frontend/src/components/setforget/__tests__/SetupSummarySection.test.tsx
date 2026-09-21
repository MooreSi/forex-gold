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
  atr: 6, daily_atr: 20, ema_fast: 1995, ema_slow: 1960, rsi: 52, fib: 0.5,
  fib_levels: [], impulse: null, confirmation: null,
};

const LONG: SetForgetCandidate = {
  direction: "BUY", entry: 1985, stop_loss: 1972, take_profit: 2040,
  order_type: "limit", risk: 13, reward: 55, rr: 4.23,
  stage: "triggered",
  trigger: { kind: "shift_of_structure", ts: 1, level: 1972 },
  distance: 15, distance_days: 0.75,
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

describe("how long a resting order will wait", () => {
  /**
   * Reported 2026-09-21: an order went out at a zone days away and nothing on
   * the page said so. A resting order that fills tomorrow and one that fills
   * in a fortnight render identically — same card, same numbers, same green
   * Execute button — so the wait has to be stated, not left to be judged off
   * the chart.
   */
  it("states the distance and the wait", () => {
    summary({ candidate: { ...LONG, distance: 15, distance_days: 0.75 } });

    expect(screen.getByText(/15\.00 points from price/)).toBeInTheDocument();
    // Three quarters of a day is eighteen hours, which is nearer a day than
    // it is to now.
    expect(screen.getByText("about a day away")).toBeInTheDocument();
  });

  it("says price is already there when it is", () => {
    summary({ candidate: { ...LONG, distance: 2, distance_days: 0.1 } });

    expect(screen.getByText("price is there now")).toBeInTheDocument();
  });

  it("says a day for a day", () => {
    summary({ candidate: { ...LONG, distance: 22, distance_days: 1.1 } });

    expect(screen.getByText("about a day away")).toBeInTheDocument();
  });

  it("warns when the wait runs to days", () => {
    const { container } = summary({
      candidate: { ...LONG, distance: 160, distance_days: 8 },
    });

    expect(screen.getByText("about 8 days away")).toBeInTheDocument();
    expect(container.querySelector(".text-warning")).not.toBeNull();
  });

  it("does not warn about a wait of hours", () => {
    const { container } = summary({
      candidate: { ...LONG, distance: 8, distance_days: 0.4 },
    });

    expect(container.querySelector(".text-warning")).toBeNull();
  });

  it("says the wait is unknown rather than implying it fills now", () => {
    /* Null is "the daily range could not be read". Rendered as "0 days" that
       would read as "fills immediately" — the most encouraging possible wrong
       answer about a trade. */
    summary({ candidate: { ...LONG, distance: 15, distance_days: null } });

    expect(screen.getByText(/wait unknown/)).toBeInTheDocument();
  });

  it("says nothing about waiting for a market order", () => {
    summary({ candidate: { ...LONG, order_type: "market", distance: 0,
                           distance_days: 0 } });

    expect(screen.queryByText(/points from price/)).not.toBeInTheDocument();
    expect(screen.getByText(/fills now at the market/)).toBeInTheDocument();
  });
});

describe("the three stages", () => {
  /**
   * Added 2026-09-21 with the 30-minute trigger. Before it a chosen zone and a
   * live order were the same thing, which is what put one days from price.
   *
   * The tone is the point here. Waiting for the 30m is the method WORKING --
   * most of the time there is no trade -- and dressing that in the same red as
   * an inverted stop teaches the operator to ignore both.
   */
  const WAITING = { ...LONG, stage: "waiting" as const, trigger: null };
  const ARMED = { ...LONG, stage: "armed" as const, trigger: null };

  it("says what it is waiting for, not that something is wrong", () => {
    summary({
      candidate: WAITING,
      invalidations: ["Price is at the zone but the 30m has not reacted yet."],
    });

    expect(screen.getByRole("status")).toHaveTextContent(
      /Not ready to place — waiting for the 30m/i);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says price has not arrived when it has not", () => {
    summary({
      candidate: ARMED,
      invalidations: ["Price has not reached the zone yet."],
    });

    expect(screen.getByRole("status")).toHaveTextContent(
      /waiting for price to reach the zone/i);
  });

  it("still lists the reason underneath", () => {
    summary({
      candidate: WAITING,
      invalidations: ["Price is at the zone but the 30m has not reacted yet."],
    });

    expect(screen.getByText(/the 30m has not reacted yet/)).toBeInTheDocument();
  });

  it("keeps the red alert for a genuine rule breach on a triggered setup", () => {
    /* The tone softening must not swallow a real refusal. A 1:0.6 ratio on a
       triggered setup is still wrong, and still red. */
    summary({
      candidate: { ...LONG, stage: "triggered" },
      invalidations: ["Reward-to-risk is 1:0.60."],
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      /does not meet the method's rules/);
  });

  it("shows nothing at all when a triggered setup is sound", () => {
    summary({ candidate: { ...LONG, stage: "triggered" }, invalidations: [] });

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
