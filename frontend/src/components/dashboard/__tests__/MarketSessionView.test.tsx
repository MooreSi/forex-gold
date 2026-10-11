import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MarketSessionView } from "../internal/MarketSessionView";
import { PriceHeroCard } from "../internal/PriceHeroCard";
import type { HeaderState } from "@/api/types";

/**
 * Owner, 2026-10-09: "on the dashboard above lifetime P&L add an icon to
 * show which trading market is currently trading in".
 *
 * The session comes from the schedule payload's `markets`, which reads it
 * through the same `is_session_allowed` the execution gate uses. So the view
 * can say two things: which market the clock is in, and whether that market
 * is one the operator has switched on.
 */
const markets = (session: string, allowed: boolean) => ({
  asia: true, london: true, new_york: true, session, allowed_now: allowed,
});

describe("the market-session view", () => {
  it("names the market and says it is trading", () => {
    render(<MarketSessionView markets={markets("london", true)} />);

    const view = screen.getByTestId("dash-market");
    expect(view).toHaveTextContent("London");
    expect(view).toHaveTextContent(/trading/i);
    expect(view.querySelector("svg")).toBeInTheDocument();
  });

  it("names both markets during the overlap", () => {
    render(<MarketSessionView markets={markets("overlap", true)} />);

    expect(screen.getByTestId("dash-market")).toHaveTextContent("London + New York");
  });

  it("says a market the operator switched off is switched off", () => {
    // The clock is in New York, but the gate refuses it. Reading "New York"
    // alone, in green, would say orders are going in when they are not.
    render(<MarketSessionView markets={markets("ny", false)} />);

    const view = screen.getByTestId("dash-market");
    expect(view).toHaveTextContent("New York");
    expect(view).toHaveTextContent(/switched off/i);
    expect(view).not.toHaveTextContent(/trading/i);
  });

  it("says the weekend is the weekend", () => {
    render(<MarketSessionView markets={markets("closed", false)} />);

    expect(screen.getByTestId("dash-market")).toHaveTextContent(/markets closed/i);
  });

  it("says the 21:00-24:00 UTC gap is outside trading hours", () => {
    render(<MarketSessionView markets={markets("off", false)} />);

    expect(screen.getByTestId("dash-market")).toHaveTextContent(/outside trading hours/i);
  });

  it("shows nothing rather than a guess when the markets are unreadable", () => {
    // `screen_extras` sends markets: null when the toggles cannot be read.
    // A default here would claim a session is live that may be switched off.
    render(<MarketSessionView markets={{}} />);

    expect(screen.queryByTestId("dash-market")).not.toBeInTheDocument();
  });
});

describe("on the price card", () => {
  const header = {
    account: { equity: 1000, balance: 1000 }, lifetime_pnl: 50,
  } as unknown as HeaderState;

  it("sits in the figures column, above Lifetime P&L", () => {
    render(<PriceHeroCard tick={null} header={header} daily={[]}
      markets={markets("asian", true)} />);

    const view = screen.getByTestId("dash-market");
    const lifetime = screen.getByText("Lifetime P&L");
    expect(view).toHaveTextContent("Asia");
    // Earlier in document order: above it on screen.
    expect(view.compareDocumentPosition(lifetime) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
  });
});
