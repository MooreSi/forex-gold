/**
 * The side the page is not showing (both directions since 2026-10-02).
 *
 * The owner's report was a page that "only ever shows a sell which it never
 * reaches" while price sat on the demand below. The backend now ranks both
 * sides; this line is how the loser stays visible instead of vanishing the way
 * the BUY did.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OtherSideSection } from "../internal/OtherSideSection";
import type { SetForgetCandidate } from "@/api/types";

const SHORT_ARMED: SetForgetCandidate = {
  direction: "SELL", entry: 4330.48, stop_loss: 4361.59, take_profit: 4129.89,
  order_type: "limit", risk: 31.11, reward: 200.59, rr: 6.45,
  stage: "armed", trigger: null, distance: 142.97, distance_days: 1.71,
};

describe("OtherSideSection", () => {
  it("names the direction, the entry, the stage and the wait", () => {
    render(<OtherSideSection candidate={SHORT_ARMED} />);

    const line = screen.getByRole("note");
    expect(line).toHaveTextContent("Also watching");
    expect(line).toHaveTextContent("SELL");
    expect(line).toHaveTextContent("4330.48");
    expect(line).toHaveTextContent("price has not reached the zone");
    expect(line).toHaveTextContent("142.97 points away");
    expect(line).toHaveTextContent("about 2 days away");
  });

  it("says when the other side has triggered too", () => {
    render(<OtherSideSection candidate={{ ...SHORT_ARMED, stage: "triggered" }} />);

    expect(screen.getByRole("note")).toHaveTextContent("triggered");
  });

  it("renders nothing when there is no other side", () => {
    const { container } = render(<OtherSideSection candidate={null} />);

    expect(container).toBeEmptyDOMElement();
  });
});
