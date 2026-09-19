/**
 * What the model made of the setup.
 *
 * One surface here matters more than the rest and is not reachable from the
 * panel's own tests: **`levels_rejected`**. It means the model proposed an
 * entry, a stop or a target that broke the method's rules, the backend threw
 * them away, and the numbers on screen are the rules' own. If that is not
 * visible the page claims a review it did not get — the operator reads a card
 * headed "AI review: Take it" and assumes the levels beneath it are the ones
 * the model chose.
 *
 * The other one: a "skip" is shown as prominently as a "take", and the setup
 * stays on screen underneath it. The model is reviewing, not deciding, and
 * disagreeing with it is a legitimate thing to do — which is impossible if the
 * objection hides the thing being objected to.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReviewSection } from "../internal/ReviewSection";
import type { SetForgetReview } from "@/api/types";

const TAKE: SetForgetReview = {
  verdict: "take",
  reasoning: "Daily demand at 1975, weekly with it.",
  risks: "NFP on Friday.",
  levels_rejected: [],
  model: "a-model",
  error: null,
};

describe("the verdict", () => {
  it("names a take and shows the reasoning", () => {
    render(<ReviewSection review={TAKE} />);

    expect(screen.getByText("AI review: Take it")).toBeInTheDocument();
    expect(screen.getByText(/Daily demand at 1975/)).toBeInTheDocument();
  });

  it("names a skip just as plainly", () => {
    render(<ReviewSection review={{ ...TAKE, verdict: "skip",
                                    reasoning: "News in an hour." }} />);

    expect(screen.getByText("AI review: Skip it")).toBeInTheDocument();
    expect(screen.getByText("News in an hour.")).toBeInTheDocument();
  });

  it("names an adjust", () => {
    render(<ReviewSection review={{ ...TAKE, verdict: "adjust" }} />);

    expect(screen.getByText("AI review: Take it, adjusted")).toBeInTheDocument();
  });

  it("says which model was billed", () => {
    render(<ReviewSection review={TAKE} />);

    expect(screen.getByText("a-model")).toBeInTheDocument();
  });

  it("surfaces the risks the model named", () => {
    render(<ReviewSection review={TAKE} />);

    expect(screen.getByText("NFP on Friday.")).toBeInTheDocument();
  });

  it("renders without a verdict rather than crashing on a partial reply", () => {
    render(<ReviewSection review={{ verdict: null, reasoning: "Unsure." }} />);

    expect(screen.getByText("AI review")).toBeInTheDocument();
    expect(screen.getByText("Unsure.")).toBeInTheDocument();
  });
});

describe("when the model's own levels were thrown away", () => {
  const REJECTED: SetForgetReview = {
    ...TAKE,
    levels_rejected: [
      "Reward-to-risk is 1:0.60. Set & Forget does not take anything under 1:2.",
    ],
  };

  it("says so, and says which rule the model broke", () => {
    render(<ReviewSection review={REJECTED} />);

    expect(screen.getByText(/levels were discarded/)).toBeInTheDocument();
    expect(screen.getByText(/1:0\.60/)).toBeInTheDocument();
  });

  it("says whose numbers are on screen instead", () => {
    /* Without this the card reads "AI review: Take it" above levels the model
       never chose, which is the page claiming a review it did not get. */
    render(<ReviewSection review={REJECTED} />);

    expect(screen.getByText(/are the rules' own, not the model's/))
      .toBeInTheDocument();
  });

  it("says nothing about discarded levels when none were", () => {
    render(<ReviewSection review={TAKE} />);

    expect(screen.queryByText(/discarded/)).not.toBeInTheDocument();
  });
});

describe("when the review did not complete", () => {
  it("reports the provider's own reason", () => {
    render(<ReviewSection review={{
      verdict: null, error: "The AI provider did not answer: upstream 529",
    }} />);

    expect(screen.getByRole("status")).toHaveTextContent("upstream 529");
  });

  it("says the page still works without one", () => {
    /* A failed billable call must not read as a broken page: everything above
       it was computed from the chart and costs nothing. */
    render(<ReviewSection review={{ verdict: null, error: "timed out" }} />);

    expect(screen.getByText(/does not depend on a model/)).toBeInTheDocument();
  });

  it("does not show a verdict alongside an error", () => {
    render(<ReviewSection review={{
      verdict: "take", reasoning: "ignore me", error: "timed out",
    }} />);

    expect(screen.queryByText(/Take it/)).not.toBeInTheDocument();
    expect(screen.queryByText("ignore me")).not.toBeInTheDocument();
  });
});
