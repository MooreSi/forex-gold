import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LadderRrSection } from "../internal/LadderRrSection";

/**
 * The reward-per-unit-risk readout under a TP ladder in the EA template
 * editor. It exists so a ladder can be judged against the stop it is actually
 * running rather than by eye, while it is being designed.
 *
 * Two numbers, because they answer different questions. "R at TP" is how far
 * a level reaches (pips / SL) and is unchanged by how much closes there;
 * "weighted R" is what that level actually contributes once only its own slice
 * of the position is banked. A ladder can look generous on the first row and
 * give most of it back on the second.
 */
function draftFor(over: Record<string, unknown> = {}) {
  return {
    sl_pips: "40", close_full_on_last: false, tp_from_telegram: false,
    tp1_pips: "40", tp1_pct: "50",
    tp2_pips: "80", tp2_pct: "30",
    tp3_pips: "130", tp3_pct: "20",
    ...over,
  };
}

function rowValues(label: string): string[] {
  const row = screen.getByRole("row", { name: new RegExp(label, "i") });
  return within(row).getAllByRole("cell").map((c) => c.textContent?.trim() ?? "");
}

describe("what a level reaches, and what it banks", () => {
  it("shows each level's distance as a multiple of the stop", () => {
    render(<LadderRrSection prefix="tp" maxLevel={3} draft={draftFor()} />);

    expect(rowValues("R at TP")).toEqual(["1.00R", "2.00R", "3.25R"]);
  });

  it("weights each level by the slice that actually closes there", () => {
    // 3.25R at TP3 contributes 0.65R, because only 20% is left to close.
    render(<LadderRrSection prefix="tp" maxLevel={3} draft={draftFor()} />);

    expect(rowValues("weighted R")).toEqual(["0.50R", "0.60R", "0.65R"]);
  });

  it("states the total and what is left running", () => {
    render(<LadderRrSection prefix="tp" maxLevel={3} draft={draftFor({ tp3_pct: "0" })} />);

    const summary = screen.getByTestId("rr-summary-tp");
    expect(summary).toHaveTextContent("1.10R");
    expect(summary).toHaveTextContent(/20% left running/i);
  });

  it("banks the remainder at the last level when close-full is on", () => {
    // The same ladder is worth more with the switch on, and the readout has to
    // say so: %s stopping short do not mean a runner unless it is off.
    render(<LadderRrSection prefix="tp" maxLevel={3}
      draft={draftFor({ tp3_pct: "0", close_full_on_last: true })} />);

    expect(screen.getByTestId("rr-summary-tp")).toHaveTextContent("1.75R");
  });
});

describe("a ladder that promises more than it has", () => {
  it("warns when the % column adds past 100 and still shows the honest total", () => {
    // 25/25/25/100 sums to 175%. TP4 can only take the 25% left behind, so
    // the ladder banks 2.50R and not the 4.00R a summed % column implies.
    render(<LadderRrSection prefix="tp" maxLevel={4} draft={{
      sl_pips: "40", close_full_on_last: true, tp_from_telegram: false,
      tp1_pips: "40", tp1_pct: "25", tp2_pips: "80", tp2_pct: "25",
      tp3_pips: "120", tp3_pct: "25", tp4_pips: "160", tp4_pct: "100",
    }} />);

    const summary = screen.getByTestId("rr-summary-tp");
    expect(summary).toHaveTextContent("2.50R");
    expect(summary).toHaveTextContent(/add to 175%/i);
  });
});

describe("when there is no answer to give", () => {
  it("asks for a stop rather than showing a number", () => {
    render(<LadderRrSection prefix="tp" maxLevel={3} draft={draftFor({ sl_pips: "0" })} />);

    expect(screen.getByTestId("rr-summary-tp")).toHaveTextContent(/stop distance/i);
    expect(screen.queryByRole("row", { name: /R at TP/i })).not.toBeInTheDocument();
  });

  it("says so when no level is set", () => {
    render(<LadderRrSection prefix="tp" maxLevel={3} draft={{
      sl_pips: "40", close_full_on_last: false, tp_from_telegram: false,
    }} />);

    expect(screen.getByTestId("rr-summary-tp")).toHaveTextContent(/no take-profit level/i);
  });
});

describe("when the signal states the distances", () => {
  it("says the readout is from the boxes, not from the signal", () => {
    // The pips boxes still drive internal-generator trades, so they keep their
    // values — but for a Telegram signal the distances come from the message
    // and this readout is a plan, not what will happen.
    render(<LadderRrSection prefix="tp" maxLevel={3}
      draft={draftFor({ tp_from_telegram: true })} />);

    expect(screen.getByTestId("rr-summary-tp")).toHaveTextContent(/from the signal/i);
  });

  it("reads the pending ladder's own switch", () => {
    render(<LadderRrSection prefix="tp_pen" maxLevel={1} draft={{
      sl_pips: "40", close_full_on_last: false, tp_from_telegram: true,
      tp_pen_from_telegram: false, tp_pen1_pips: "80", tp_pen1_pct: "100",
    }} />);

    expect(screen.getByTestId("rr-summary-tp_pen")).not.toHaveTextContent(/from the signal/i);
  });
});
