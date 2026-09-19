/**
 * The long/short position box.
 *
 * It is a picture of a trade, and a picture that is wrong is worse than no
 * picture: the operator reads it, agrees with it and presses Execute. Two
 * failures matter more than the rest.
 *
 * **The bands must follow the trade, not the screen.** A long's target is
 * above its entry and a short's is below, so "green band on top" is only right
 * half the time. A short rendered with the colours the other way round reads
 * as the opposite trade and every number beside it still agrees.
 *
 * **No lot size means no cash figure.** $0.00 is a real answer meaning "this
 * trade wins nothing" and it is not the same as not having chosen a size yet.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PositionOverlay } from "../internal/PositionOverlay";

/** A long: target above the entry, so a SMALLER y. Stop below, so larger. */
const LONG = {
  direction: "BUY" as const,
  entry: 2000, stopLoss: 1980, takeProfit: 2060,
  entryY: 200, stopY: 260, targetY: 20,
  left: 300, right: 500,
  riskMoney: 200, rewardMoney: 600, rr: 3,
};

/** The mirror: target BELOW the entry, so a larger y. */
const SHORT = {
  ...LONG,
  direction: "SELL" as const,
  entry: 2000, stopLoss: 2020, takeProfit: 1940,
  entryY: 200, stopY: 140, targetY: 380,
};

function bands() {
  const overlay = screen.getByTestId("position-overlay");
  const all = Array.from(overlay.querySelectorAll("div[style]"));
  return {
    profit: all.find((d) => d.className.includes("bg-profit")) as HTMLElement,
    loss: all.find((d) => d.className.includes("bg-loss")) as HTMLElement,
  };
}

describe("the bands follow the trade, not the screen", () => {
  it("draws a long's profit band above its entry and its loss band below", () => {
    render(<PositionOverlay {...LONG} />);
    const { profit, loss } = bands();

    expect(profit.style.top).toBe("20px");
    expect(profit.style.height).toBe("180px");
    expect(loss.style.top).toBe("200px");
    expect(loss.style.height).toBe("60px");
  });

  it("draws a short's profit band below its entry", () => {
    render(<PositionOverlay {...SHORT} />);
    const { profit, loss } = bands();

    expect(profit.style.top).toBe("200px");
    expect(profit.style.height).toBe("180px");
    expect(loss.style.top).toBe("140px");
    expect(loss.style.height).toBe("60px");
  });

  it("spans the box between the two x edges it was given", () => {
    render(<PositionOverlay {...LONG} />);

    expect(bands().profit.style.width).toBe("200px");
    expect(bands().profit.style.left).toBe("300px");
  });
});

describe("the numbers on the flags", () => {
  it("names the target, its distance and its percentage", () => {
    render(<PositionOverlay {...LONG} />);

    expect(screen.getByText(/Target: 2060\.00/)).toBeInTheDocument();
    expect(screen.getByText(/\+3\.00%/)).toBeInTheDocument();
    expect(screen.getByText(/60\.00 pts/)).toBeInTheDocument();
  });

  it("shows a short's target as a negative percentage", () => {
    render(<PositionOverlay {...SHORT} />);

    expect(screen.getByText(/Target: 1940\.00/)).toBeInTheDocument();
    expect(screen.getByText(/-3\.00%/)).toBeInTheDocument();
  });

  it("prints the cash at each end for the chosen size", () => {
    render(<PositionOverlay {...LONG} />);

    expect(screen.getByText(/Target:.*\$600\.00/)).toBeInTheDocument();
    expect(screen.getByText(/Stop:.*\$200\.00/)).toBeInTheDocument();
  });

  it("omits the cash entirely when no size has been chosen", () => {
    render(<PositionOverlay {...LONG} riskMoney={null} rewardMoney={null} />);

    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.getByText(/Target: 2060\.00/)).toBeInTheDocument();
  });

  it("carries the reward-to-risk ratio on the entry line", () => {
    render(<PositionOverlay {...LONG} />);

    expect(screen.getByText(/Risk\/reward 1:3\.00/)).toBeInTheDocument();
    expect(screen.getByText(/BUY entry 2000\.00/)).toBeInTheDocument();
  });

  it("says so rather than inventing a ratio when there is none", () => {
    render(<PositionOverlay {...LONG} rr={null} />);

    expect(screen.getByText(/Risk\/reward —/)).toBeInTheDocument();
  });
});
