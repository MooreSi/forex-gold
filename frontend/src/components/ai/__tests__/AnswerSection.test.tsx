import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerSection, parseAnswer } from "../internal/AnswerSection";

/**
 * The model's answer, broken into the sections it was asked for.
 *
 * `_SIGNAL_GEN_SYSTEM` ends with "Respond ONLY with a single minified JSON
 * object matching this exact schema", so what the panel was rendering in a
 * pre-wrap block was a minified JSON object, on screen, as text. That is what
 * the owner meant by "ai analysis should be broken down similar to the nicegui
 * version" — the NiceGUI page rendered the same answer as a score card and a
 * section per engine.
 *
 * The rule that makes this safe: a prose answer is SHOWN, never hidden. A
 * model can always return a refusal or an apology, and an answer the operator
 * paid for must reach the screen whatever shape it arrived in.
 */
const ANSWER = JSON.stringify({
  overall_assessment: "Both engines are learning, slowly.",
  engines: [
    {
      name: "Breakout Engine", verdict: "Improving on fakeout classification.",
      trend: "improving", ml_contribution: "The classifier is adding 4pp of win rate.",
      self_learning_progress: "412 labelled outcomes so far.",
      acting_like_pro_trader: false,
      key_strength: "Range detection is solid.",
      key_weakness: "Enters too early on the retest.",
      recommendation: "Wait for the close beyond the range.",
    },
    {
      name: "Reversal Engine", verdict: "Stalled.", trend: "declining",
      ml_contribution: "No measurable effect yet.",
      self_learning_progress: "Corpus is one-sided.",
      acting_like_pro_trader: true,
      key_strength: "Level detection.", key_weakness: "Sizing.",
      recommendation: "Balance the corpus.",
    },
  ],
  collective_verdict: "Not yet professional, but not random either.",
  what_would_make_them_professional: "Consistent sizing and a real exit model.",
});

describe("reading the answer", () => {
  it("parses the minified JSON the prompt asks for", () => {
    expect(parseAnswer(ANSWER)?.["collective_verdict"]).toBe(
      "Not yet professional, but not random either.");
  });

  it("parses an answer the model wrapped in a code fence", () => {
    // A model that fences its JSON has still answered.
    expect(parseAnswer("```json\n" + ANSWER + "\n```")).not.toBeNull();
  });

  it("does not try to parse prose", () => {
    expect(parseAnswer("I cannot analyse this without more data.")).toBeNull();
  });

  it("does not try to parse a bare list", () => {
    // Valid JSON, wrong shape. Rendering it as an object would blank the
    // panel rather than show what came back.
    expect(parseAnswer("[1, 2, 3]")).toBeNull();
  });

  it("does not throw on truncated JSON", () => {
    // A model that ran out of tokens mid-object is a real outcome.
    expect(parseAnswer('{"overall_assessment": "it was go')).toBeNull();
  });

  it("treats an empty answer as nothing to parse", () => {
    expect(parseAnswer("")).toBeNull();
  });
});

describe("what it renders", () => {
  it("leads with the overall assessment", () => {
    render(<AnswerSection answer={ANSWER} />);

    expect(screen.getByTestId("overall-assessment"))
      .toHaveTextContent("Both engines are learning");
  });

  it("gives each engine its own section", () => {
    render(<AnswerSection answer={ANSWER} />);

    expect(screen.getByTestId("engine-verdict-Breakout Engine")).toBeInTheDocument();
    expect(screen.getByTestId("engine-verdict-Reversal Engine")).toBeInTheDocument();
  });

  it("shows every field the schema asks the model for", () => {
    // A field the model was told to fill and the screen does not show is a
    // paid answer thrown away.
    render(<AnswerSection answer={ANSWER} />);
    const card = screen.getByTestId("engine-verdict-Breakout Engine");

    for (const text of ["Improving on fakeout classification.",
                        "The classifier is adding 4pp of win rate.",
                        "412 labelled outcomes so far.",
                        "Range detection is solid.",
                        "Enters too early on the retest.",
                        "Wait for the close beyond the range."]) {
      expect(card).toHaveTextContent(text);
    }
  });

  it("reads a stringified boolean as not professional", () => {
    // A model returning "false" as a STRING is truthy in JavaScript. The same
    // strict-comparison rule the demo/live badge carries, for the same
    // reason: the flattering answer must not be the accidental one.
    render(<AnswerSection answer={JSON.stringify({
      engines: [{ name: "Breakout Engine", acting_like_pro_trader: "false" }],
    })} />);

    expect(screen.getByTestId("pro-Breakout Engine"))
      .toHaveTextContent("not yet a professional");
  });

  it("says whether an engine is trading like a professional", () => {
    render(<AnswerSection answer={ANSWER} />);

    expect(screen.getByTestId("pro-Reversal Engine"))
      .toHaveTextContent("trading like a professional");
    expect(screen.getByTestId("pro-Breakout Engine"))
      .toHaveTextContent("not yet a professional");
  });

  it("shows the trend in words as well as colour", () => {
    // A green arrow alone cannot be read by anything but an eye.
    render(<AnswerSection answer={ANSWER} />);

    expect(screen.getByTestId("engine-verdict-Breakout Engine"))
      .toHaveTextContent("improving");
  });

  it("shows the two closing verdicts", () => {
    render(<AnswerSection answer={ANSWER} />);

    expect(screen.getByTestId("collective-verdict")).toHaveTextContent("not random either");
    expect(screen.getByTestId("what-would-make-them-professional"))
      .toHaveTextContent("Consistent sizing");
  });
});

describe("an answer that is not the schema", () => {
  it("shows prose as it came back", () => {
    render(<AnswerSection answer="There is not enough data to say anything useful." />);

    expect(screen.getByTestId("ai-answer"))
      .toHaveTextContent("not enough data");
  });

  it("shows a partial object without inventing the missing parts", () => {
    // No `engines` key, so this takes the generic path -- which is correct:
    // the engine-card layout is for the signal-generator schema, and an
    // object that is not that schema is still an answer.
    render(<AnswerSection answer={JSON.stringify({ overall_assessment: "Thin." })} />);

    expect(screen.getByTestId("ai-answer")).toHaveTextContent("Thin.");
    expect(screen.queryByTestId("answer-collective_verdict")).not.toBeInTheDocument();
  });

  it("survives an engines field that is not a list", () => {
    render(<AnswerSection answer={JSON.stringify({ engines: "none" })} />);

    expect(screen.getByTestId("ai-answer")).toBeInTheDocument();
  });
});
