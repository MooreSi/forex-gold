import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ModelSection } from "../internal/ModelSection";

/**
 * What the pro model's AUC does and does not say (2026-10-02).
 *
 * 0.82 read as "predicts winners". It separates the moments a channel posted
 * from background snapshots, and on random folds it partly learned the date.
 * Scored on its own, its output ranks our trades' outcomes at AUC 0.496.
 */
const MODEL = { ready: true, auc: 0.581, auc_forward: 0.577, n: 12973,
  min_auc: 0.55, corpus: {} };

describe("the pro model's AUC", () => {
  it("says what the model separates, and that it does not say who wins", () => {
    render(<ModelSection model={MODEL} />);

    expect(screen.getByTestId("model-auc-meaning"))
      .toHaveTextContent(/posted from background/i);
    expect(screen.getByTestId("model-auc-meaning"))
      .toHaveTextContent(/not whether a trade wins/i);
  });

  it("shows the forward-in-time AUC beside the blocked-fold one", () => {
    render(<ModelSection model={MODEL} />);

    expect(screen.getByTestId("model-auc-forward")).toHaveTextContent("0.577");
  });

  it("shows no forward figure rather than 0.000 when it was not measured", () => {
    render(<ModelSection model={{ ...MODEL, auc_forward: null }} />);

    expect(screen.queryByTestId("model-auc-forward")).not.toBeInTheDocument();
  });
});
