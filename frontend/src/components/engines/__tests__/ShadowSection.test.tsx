import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ShadowSection } from "../internal/ShadowSection";

/**
 * The virtual trades: what each configuration would have done.
 *
 * Asked for on 2026-09-19 — "virtual trade history so we can monitor how they
 * are performing". The machinery recorded it all along; nothing read it back
 * except an aggregate.
 *
 * The distinction these tests protect is between a variant that SKIPPED a
 * losing trade and one that never saw it. Both contribute nothing to the P&L
 * and only one of them is evidence.
 */
const SHADOW = [
  { variant: "champion", is_champion: true, n_taken: 40, n_skipped: 4,
    net: 310.5, mean_r: 0.42 },
  { variant: "challenger", is_champion: false, n_taken: 18, n_skipped: 26,
    net: -88.0, mean_r: null },
];

const HISTORY = [
  { ts: 1757955600, signal_ref: "s2", variant: "challenger", would_take: 0,
    reason: "adx below 22", direction: "SELL", status: "closed",
    outcome: "loss", net: -25, r: -1 },
  { ts: 1757952000, signal_ref: "s1", variant: "champion", would_take: 1,
    reason: "adx ok", direction: "BUY", status: "closed", outcome: "win",
    net: 50, r: 2 },
  { ts: 1757900000, signal_ref: "s3", variant: "champion", would_take: 1,
    reason: "", direction: "BUY", status: "pending", outcome: "open",
    net: null, r: null },
];

// The shape `panel_data.get_realised_pnl` really returns, checked against the
// live payload on 2026-09-19. The first version of this test invented
// `{net_pnl}` and passed against a component reading the same invented key --
// which is precisely how the pro-model section went a year reporting nothing.
const REALISED = { n: 58, total: -206.62, per_trade: -3.5624 };

const render_ = (over: Record<string, unknown> = {}) =>
  render(<ShadowSection shadow={SHADOW} history={HISTORY}
    realised={REALISED} {...over} />);

describe("the scoreboard", () => {
  it("marks which variant is the live one", async () => {
    // Without it the table is two configurations and no indication which one
    // is currently deciding real trades.
    render_();

    expect(within(screen.getByTestId("variant-champion")).getByText("live"))
      .toBeInTheDocument();
    expect(within(screen.getByTestId("variant-challenger")).queryByText("live"))
      .not.toBeInTheDocument();
  });

  it("counts what each variant took and skipped", async () => {
    render_();
    const row = screen.getByTestId("variant-challenger");

    expect(row).toHaveTextContent("18");
    expect(row).toHaveTextContent("26");
  });

  it("leaves mean R blank where there is nothing to score", async () => {
    // Zero expectancy and no evidence are different statements, and 0.000
    // for both invites the wrong one to be acted on.
    render_();

    expect(within(screen.getByTestId("variant-challenger")).queryByText("0.000"))
      .not.toBeInTheDocument();
  });

  it("shows the engine's real P&L beside the virtual one", async () => {
    // So a variant that looks brilliant on paper can be read against what the
    // engine actually did.
    render_();

    expect(screen.getByTestId("shadow-realised")).toHaveTextContent("-$206.62");
  });

  it("says how many real trades that P&L is over", async () => {
    // -$206 over 58 trades and -$206 over 3 are different statements.
    render_();

    expect(screen.getByTestId("shadow-realised")).toHaveTextContent("58");
  });

  it("says nothing rather than zero when the engine has no realised P&L", async () => {
    render_({ realised: {} });

    expect(screen.queryByTestId("shadow-realised")).not.toBeInTheDocument();
  });
});

describe("the decision history", () => {
  it("shows a skip as a row of its own", async () => {
    render_();

    expect(screen.getByTestId("decision-s2-challenger")).toHaveTextContent("skipped");
  });

  it("says why a variant skipped", async () => {
    render_();

    expect(screen.getByTestId("decision-s2-challenger"))
      .toHaveTextContent("adx below 22");
  });

  it("shows the R a taken decision earned", async () => {
    render_();

    expect(screen.getByTestId("decision-s1-champion")).toHaveTextContent("2.00");
  });

  it("leaves R blank on a signal that has not settled", async () => {
    render_();

    const row = screen.getByTestId("decision-s3-champion");
    expect(row).toHaveTextContent("pending");
    expect(row).not.toHaveTextContent("0.00");
  });
});

describe("when there is nothing yet", () => {
  it("says no variant has seen a closed signal", async () => {
    render_({ shadow: [] });

    expect(screen.getByText(/No variant has seen a closed signal/)).toBeInTheDocument();
  });

  it("says no decisions have been recorded", async () => {
    render_({ history: [] });

    expect(screen.getByText(/No decisions recorded yet/)).toBeInTheDocument();
  });

  it("survives a payload that is not a list", async () => {
    render_({ shadow: {}, history: null });

    expect(screen.getByText(/No variant has seen a closed signal/)).toBeInTheDocument();
  });
});
