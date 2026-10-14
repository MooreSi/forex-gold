/**
 * What the model learns from, under the "Is it learning?" chart
 * (docs/todo/017). The four learning switches change the label, the weights
 * and whether real trades count; without this line the chart looks the same
 * whichever is on.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LearningSourceLine } from "../internal/LearningSourceLine";

describe("LearningSourceLine", () => {
  it("names the label, the weights and what they are worth", () => {
    render(
      <LearningSourceLine
        learning={{
          label: "realised R",
          weights: "positional (newest rows weighted most)",
          effective_n: 117.6,
        }}
      />,
    );
    const line = screen.getByTestId("learning-source");
    expect(line).toHaveTextContent("realised R");
    expect(line).toHaveTextContent("positional (newest rows weighted most)");
    expect(line).toHaveTextContent("worth about 118 rows");
  });

  it("says when real trades are plotted and whether the model learns from them", () => {
    render(
      <LearningSourceLine
        learning={{ label: "x", weights: "y", executed_plotted: 315, learns_from_executed: false }}
      />,
    );
    expect(screen.getByTestId("learning-source")).toHaveTextContent(
      "315 real trades plotted; the model does not learn from them",
    );
  });

  it("says when the model has no opinion yet", () => {
    render(
      <LearningSourceLine
        learning={{ label: "realised R", weights: "y", armed: false, holdout_auc: 0.53 }}
      />,
    );
    expect(screen.getByTestId("learning-source")).toHaveTextContent(
      "No opinion yet (newest-quarter AUC 0.53)",
    );
  });

  it("draws nothing when the report has no learning block", () => {
    const { container } = render(<LearningSourceLine learning={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });
});
