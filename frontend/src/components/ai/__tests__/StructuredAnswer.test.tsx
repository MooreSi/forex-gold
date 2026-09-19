import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { humanise, StructuredAnswer } from "../internal/StructuredAnswer";

/**
 * Any JSON answer, as labelled sections.
 *
 * There are three analysis subjects and three schemas. A renderer per schema
 * means the fourth subject arrives as raw JSON on screen — which is exactly
 * the state this whole tab was in until 2026-09-19. So this draws the SHAPE:
 * a string is a paragraph, a nested object is a block, a list of objects is a
 * table.
 *
 * It must be incapable of hiding a field. A field the prompt paid for and the
 * screen discards is the failure it replaced.
 */
const CHANNEL_ANSWER = {
  reliability_score: 72,
  reliability_label: "Reliable with caveats",
  executive_summary: "Strong entries, stops too tight.",
  phantom_tps: [
    { date: "2026-09-12", direction: "BUY", claimed: "TP2", actual: "SL", pnl: -42.5 },
  ],
  sl_management: {
    channel_instructs_be: false,
    current_weakness: "No BE instruction.",
    recommended_rule: "Move to BE at TP1.",
  },
  rr_analysis: { signals_below_1_to_1: 4, comment: "Four are sub-1R.", flags: ["tight stops"] },
};

describe("naming the fields", () => {
  it("turns a key into words", () => {
    expect(humanise("executive_summary")).toBe("Executive summary");
  });

  it("leaves the app's acronyms alone", () => {
    // "Rr analysis" and "Dpm assessment" read as typos.
    expect(humanise("rr_analysis")).toBe("R:R analysis");
    expect(humanise("dpm_assessment")).toBe("DPM assessment");
    expect(humanise("sl_management")).toBe("SL management");
  });

  it("survives a key it cannot improve", () => {
    expect(humanise("")).toBe("");
  });
});

describe("what it renders", () => {
  it("shows every field the model returned", () => {
    // The rule. Anything asked for and answered reaches the screen.
    render(<StructuredAnswer answer={CHANNEL_ANSWER} />);

    for (const key of ["executive_summary", "phantom_tps", "sl_management",
                       "rr_analysis"]) {
      expect(screen.getByTestId(`answer-${key}`)).toBeInTheDocument();
    }
  });

  it("makes the reliability score the headline", () => {
    render(<StructuredAnswer answer={CHANNEL_ANSWER} />);
    const score = screen.getByTestId("answer-reliability_score");

    expect(score).toHaveTextContent("72");
    expect(score).toHaveTextContent("Reliable with caveats");
  });

  it("does not repeat the label as a section of its own", () => {
    render(<StructuredAnswer answer={CHANNEL_ANSWER} />);

    expect(screen.queryByTestId("answer-reliability_label")).not.toBeInTheDocument();
  });

  it("renders a list of objects as a table", () => {
    render(<StructuredAnswer answer={CHANNEL_ANSWER} />);
    const section = screen.getByTestId("answer-phantom_tps");

    expect(within(section).getByText("2026-09-12")).toBeInTheDocument();
    expect(within(section).getByText("Claimed")).toBeInTheDocument();
  });

  it("keeps a column a later row introduced", () => {
    // A model that omits a field on the first row and gives it on the second
    // is ordinary. Taking the columns from row zero drops it silently, and
    // the value lands under the wrong heading or not at all.
    render(<StructuredAnswer answer={{ rows: [
      { date: "2026-09-12" },
      { date: "2026-09-13", pnl: -42.5 },
    ] }} />);
    const section = screen.getByTestId("answer-rows");

    expect(within(section).getByText("P&L")).toBeInTheDocument();
    expect(within(section).getByText("-42.50")).toBeInTheDocument();
  });

  it("renders a nested object as its own sub-fields", () => {
    render(<StructuredAnswer answer={CHANNEL_ANSWER} />);
    const section = screen.getByTestId("answer-sl_management");

    expect(within(section).getByText("Move to BE at TP1.")).toBeInTheDocument();
    expect(within(section).getByText("Recommended rule")).toBeInTheDocument();
  });

  it("renders a boolean as a word, not as nothing", () => {
    // `false` rendered bare in JSX is an empty string, which reads as a field
    // the model did not answer.
    render(<StructuredAnswer answer={CHANNEL_ANSWER} />);

    expect(within(screen.getByTestId("answer-sl_management")).getByText("no"))
      .toBeInTheDocument();
  });

  it("renders a list of strings as a list", () => {
    render(<StructuredAnswer answer={CHANNEL_ANSWER} />);

    expect(within(screen.getByTestId("answer-rr_analysis"))
      .getByText("tight stops")).toBeInTheDocument();
  });
});

describe("the empty and the odd", () => {
  it("says an empty list is empty rather than showing nothing", () => {
    render(<StructuredAnswer answer={{ phantom_tps: [] }} />);

    expect(screen.getByTestId("answer-phantom_tps")).toHaveTextContent("none");
  });

  it("shows a dash for a null rather than the word null", () => {
    render(<StructuredAnswer answer={{ verdict: null }} />);

    expect(screen.getByTestId("answer-verdict")).toHaveTextContent("—");
  });

  it("renders a mixed list without dropping the non-objects", () => {
    render(<StructuredAnswer answer={{ notes: ["a string", { k: "an object" }] }} />);

    expect(screen.getByTestId("answer-notes")).toHaveTextContent("a string");
  });

  it("renders an empty answer without throwing", () => {
    render(<StructuredAnswer answer={{}} />);

    expect(screen.getByTestId("structured-answer")).toBeInTheDocument();
  });

  it("shows a whole number without inventing decimals", () => {
    render(<StructuredAnswer answer={{ count: 12 }} />);

    expect(screen.getByTestId("answer-count")).toHaveTextContent("12");
  });
});

describe("the DPM schema, which has no bespoke renderer at all", () => {
  it("renders end to end", () => {
    render(<StructuredAnswer answer={{
      overall_verdict: "DPM is ahead.",
      best_approach: "dpm",
      dpm_assessment: { verdict: "Working.", recommendation: "keep_dpm" },
      strategy_notes: [{ strategy: "scale_out", verdict: "Behind DPM." }],
      actionable_advice: "Leave it on.",
    }} />);

    expect(screen.getByTestId("answer-overall_verdict")).toHaveTextContent("DPM is ahead.");
    expect(within(screen.getByTestId("answer-strategy_notes"))
      .getByText("scale_out")).toBeInTheDocument();
    expect(screen.getByTestId("answer-actionable_advice")).toHaveTextContent("Leave it on.");
  });
});
