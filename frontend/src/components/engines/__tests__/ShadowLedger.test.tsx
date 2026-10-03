import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ShadowSection } from "../internal/ShadowSection";

/**
 * The virtual ledger, one row per signal (2026-10-02).
 *
 * The owner read the same dollar figure down a column and concluded the
 * numbers were invented. They were one trade repeated once per variant. The
 * ledger states a signal's result once and puts each variant's call beside it.
 */
const SHADOW = [
  { variant: "live (champion)", is_champion: true, n_taken: 305, n_skipped: 450,
    net: -558.4, avoided_net: -3443.2, mean_net: -1.83, mean_r_full: -0.04,
    mean_r: -0.1, delta_vs_champion: 0 },
  { variant: "no filter", is_champion: false, n_taken: 755, n_skipped: 0,
    net: -4001.6, avoided_net: 0, mean_net: -5.3, mean_r_full: null,
    mean_r: null, delta_vs_champion: -3443.2 },
];

const LEDGER = [
  { signal_ref: "s1", ts: 1757955600, direction: "BUY", status: "closed",
    outcome: "win", net: 21.4, r_full: 0.31, r_replay: 0.42, executed: false,
    calls: {
      "live (champion)": { take: true, reason: "" },
      "no filter": { take: true, reason: "" },
    } },
  { signal_ref: "s2", ts: 1757952000, direction: "SELL", status: "closed",
    outcome: "loss", net: -558.4, r_full: -1.2, r_replay: -1.115, executed: false,
    calls: {
      "live (champion)": { take: false, reason: "ML -0.400 below floor 0.00" },
      "no filter": { take: true, reason: "" },
    } },
  { signal_ref: "s3", ts: 1757900000, direction: "BUY", status: "pending",
    outcome: "open", net: null, r_full: null, r_replay: null, executed: false,
    calls: { "live (champion)": { take: true, reason: "" } } },
  { signal_ref: "s4", ts: 1757800000, direction: "BUY", status: "closed",
    outcome: "loss", net: -50, r_full: null, r_replay: null, executed: true,
    calls: { "live (champion)": { take: true, reason: "" } } },
];

const show = (over: Record<string, unknown> = {}) =>
  render(<ShadowSection shadow={SHADOW} history={[]} ledger={LEDGER}
    realised={{}} {...over} />);

describe("the ledger", () => {
  it("states a signal's dollars once, not once per variant", () => {
    show();

    const row = screen.getByTestId("ledger-s2");
    expect(row.textContent!.match(/558\.40/g)).toHaveLength(1);
  });

  it("puts each variant's call beside the result", () => {
    show();
    const row = screen.getByTestId("ledger-s2");

    expect(within(row).getByTestId("call-s2-live (champion)")).toHaveTextContent("skipped");
    expect(within(row).getByTestId("call-s2-no filter")).toHaveTextContent("took");
  });

  it("says why a variant skipped", () => {
    show();

    expect(screen.getByTestId("call-s2-live (champion)"))
      .toHaveAttribute("title", "ML -0.400 below floor 0.00");
  });

  it("shows the whole trade's R and the replay's R side by side", () => {
    show();
    const row = screen.getByTestId("ledger-s1");

    expect(row).toHaveTextContent("0.31");
    expect(row).toHaveTextContent("0.42");
  });

  it("leaves the result blank on a signal that has not settled", () => {
    show();
    const row = screen.getByTestId("ledger-s3");

    expect(row).toHaveTextContent("pending");
    expect(row).not.toHaveTextContent("0.00");
    expect(row).not.toHaveTextContent("$0");
  });

  it("marks a broker-executed trade rather than showing an R it cannot know", () => {
    show();
    const row = screen.getByTestId("ledger-s4");

    expect(row).toHaveTextContent("broker");
    expect(row).toHaveTextContent("-$50.00");
  });
});

describe("the scoreboard's comparison", () => {
  it("shows what each variant avoided and how far it is from the live one", () => {
    show();
    const row = screen.getByTestId("variant-no filter");

    expect(row).toHaveTextContent("-$3,443.20");
  });

  it("leaves a mean that has nothing to score blank, never 0.000", () => {
    show();

    expect(within(screen.getByTestId("variant-no filter")).queryByText("0.000"))
      .not.toBeInTheDocument();
  });
});

describe("with no ledger", () => {
  it("shows no ledger table rather than an empty one", () => {
    show({ ledger: [] });

    expect(screen.queryByTestId("ledger-table")).not.toBeInTheDocument();
  });
});
