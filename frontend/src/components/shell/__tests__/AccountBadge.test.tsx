import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AccountBadge } from "../AccountBadge";

/**
 * "Make demo vs live unmistakable" is a money rule, and the third state is the
 * one that matters: a bridge that has not answered must never render as DEMO.
 */
describe("the account badge", () => {
  it("says LIVE for a live account", () => {
    render(<AccountBadge account={{ is_demo: false, login: 900123 }} />);
    expect(screen.getByTestId("account-badge")).toHaveAttribute("data-account-kind", "live");
    expect(screen.getByText(/LIVE/)).toBeInTheDocument();
  });

  it("says DEMO for a demo account", () => {
    render(<AccountBadge account={{ is_demo: true, login: 5203117 }} />);
    expect(screen.getByTestId("account-badge")).toHaveAttribute("data-account-kind", "demo");
  });

  it("says UNKNOWN when the bridge has not answered — never DEMO", () => {
    render(<AccountBadge account={null} />);
    const badge = screen.getByTestId("account-badge");
    expect(badge).toHaveAttribute("data-account-kind", "unknown");
    expect(badge).not.toHaveTextContent("DEMO");
  });

  it("says UNKNOWN when the field is present but not a boolean", () => {
    // A string "false" is truthy in JavaScript. Without the strict comparison
    // this renders as a demo account.
    render(<AccountBadge account={{ is_demo: "false" }} />);
    expect(screen.getByTestId("account-badge")).toHaveAttribute("data-account-kind", "unknown");
  });
});
