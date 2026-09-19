import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ModelSection } from "../internal/ModelSection";

/**
 * The pro-signal model's real status.
 *
 * This section used to read `trained` and `samples`. `pro_model.status()` has
 * never returned either — it returns `ready`, `auc`, `n`, `reason`,
 * `fitted_at`, `corpus` and `min_auc` — so the panel said "not trained yet" on
 * every install regardless, and hid a live account's 10,116 labelled samples,
 * its AUC gate and its stated reason. The two tests that covered the old
 * section asserted on the invented shape, which is why nothing went red.
 *
 * Same class as the Trading tab reading `circuit_breaker.tripped`, a key that
 * has never existed.
 */
const READY = {
  ready: true, auc: 0.62, n: 4120, reason: "", fitted_at: 1757955600,
  corpus: { pos: 1926, neg: 8190, wins: 745, losses: 92, pending: 831 },
  min_auc: 0.55,
};

describe("whether it is in use", () => {
  it("says a fitted model is in use", () => {
    render(<ModelSection model={READY} />);

    expect(screen.getByTestId("model-state")).toHaveTextContent("in use");
  });

  it("says an unfitted model is not", () => {
    render(<ModelSection model={{ ...READY, ready: false }} />);

    expect(screen.getByTestId("model-state")).toHaveTextContent("not in use");
  });

  it("gives the reason it is not in use", () => {
    // "Not ready" with no reason is the difference between "give it time" and
    // "it will never fit because the corpus is one-sided".
    render(<ModelSection model={{ ...READY, ready: false, reason: "not fitted" }} />);

    expect(screen.getByTestId("model-reason")).toHaveTextContent("not fitted");
  });

  it("does not show a reason when the model is working", () => {
    render(<ModelSection model={READY} />);

    expect(screen.queryByTestId("model-reason")).not.toBeInTheDocument();
  });
});

describe("the numbers", () => {
  it("shows the AUC against the gate it has to clear", () => {
    render(<ModelSection model={READY} />);

    expect(screen.getByText("0.620")).toBeInTheDocument();
    expect(screen.getByText(/gate 0.55/)).toBeInTheDocument();
  });

  it("shows a dash rather than a zero for an AUC that does not exist yet", () => {
    // 0.000 is a specific, terrible model. "Never measured" is not.
    render(<ModelSection model={{ ...READY, auc: null }} />);

    expect(screen.queryByText("0.000")).not.toBeInTheDocument();
  });

  it("shows the corpus, which is what the panel was hiding", () => {
    render(<ModelSection model={READY} />);

    expect(screen.getByText("10,116")).toBeInTheDocument();
    expect(screen.getByText(/1926 pro \/ 8190 not/)).toBeInTheDocument();
  });

  it("separates settled outcomes from the ones still open", () => {
    render(<ModelSection model={READY} />);

    expect(screen.getByText("837")).toBeInTheDocument();
    expect(screen.getByText(/831 still open/)).toBeInTheDocument();
  });

  it("says when it was last fitted", () => {
    render(<ModelSection model={READY} />);

    expect(screen.getByText(/Last fitted at/)).toBeInTheDocument();
  });

  it("says plainly when it never has been", () => {
    render(<ModelSection model={{ ...READY, fitted_at: 0 }} />);

    expect(screen.getByText(/Never fitted on this install/)).toBeInTheDocument();
  });
});

describe("what it survives", () => {
  it("renders with an empty payload rather than blanking the tab", () => {
    // A half-deployed backend answers {}. Reading `.pos` off an absent corpus
    // throws inside render and takes the dashboard down with it.
    render(<ModelSection model={{}} />);

    expect(screen.getByTestId("model-state")).toHaveTextContent("not in use");
  });

  it("renders with a corpus that is not an object", () => {
    render(<ModelSection model={{ ...READY, corpus: null }} />);

    expect(screen.getByTestId("model-state")).toBeInTheDocument();
  });
});
