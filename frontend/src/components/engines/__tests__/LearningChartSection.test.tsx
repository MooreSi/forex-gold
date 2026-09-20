/**
 * The "is it learning?" chart, on both engine tabs.
 *
 * The NiceGUI panels had it; the React port never drew it, so neither engine
 * could be seen to be improving or decaying. Asked for on 2026-09-20.
 *
 * Two things it must not do. It must not draw a chart out of nothing — an
 * engine with no closed signals has not "flatlined at zero", it has no
 * measurements — and it must not let the two lines read as comparable, since
 * they are different quantities on different scales in one box.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LearningChartSection } from "../internal/LearningChartSection";

const METRICS = {
  signal_ids: ["a", "b", "c", "d"],
  win_flag_series: [1, 0, 1, 1],
  actual_r_series: [1.1, -1, 0.9, 1.4],
  accuracy: 0.6,
};

describe("with closed signals to plot", () => {
  it("draws the win-rate line", () => {
    render(<LearningChartSection metrics={METRICS} summary={{}} />);

    expect(screen.getByTestId("learning-win-rate")).toBeInTheDocument();
  });

  it("draws the realised-R line", () => {
    render(<LearningChartSection metrics={METRICS} summary={{}} />);

    expect(screen.getByTestId("learning-actual-r")).toBeInTheDocument();
  });

  it("says the two scales are not comparable", () => {
    // The owner's question about the NiceGUI chart was whether the lines
    // should converge. They should not: they are unrelated quantities.
    render(<LearningChartSection metrics={METRICS} summary={{}} />);

    expect(screen.getByText(/not comparable/i)).toBeInTheDocument();
  });

  it("says how many closed signals the window rolls over", () => {
    render(<LearningChartSection metrics={METRICS} summary={{}} />);

    expect(screen.getByText(/rolling 50 closed signals/i)).toBeInTheDocument();
  });

  it("says the x axis is signals and not time", () => {
    // Reading it as a time series is the most likely wrong conclusion to
    // draw from it: signals are not evenly spaced.
    render(<LearningChartSection metrics={METRICS} summary={{}} />);

    expect(screen.getByText(/not time/i)).toBeInTheDocument();
  });
});

describe("with nothing to plot", () => {
  it("says so instead of drawing an empty box", () => {
    render(<LearningChartSection metrics={{}} summary={{}} />);

    expect(screen.getByText(/nothing to plot/i)).toBeInTheDocument();
    expect(screen.queryByTestId("learning-win-rate")).toBeNull();
  });

  it("says how many more closed signals training needs", () => {
    // "Nothing to plot" alone reads as broken. "Needs 37 more" is the engine
    // working as designed.
    render(
      <LearningChartSection
        metrics={{}}
        summary={{ trained: false, labeled_count: 3, min_needed: 40 }}
      />,
    );

    expect(screen.getByText(/37 more closed signals/i)).toBeInTheDocument();
  });

  it("does not claim a shortfall it cannot compute", () => {
    render(<LearningChartSection metrics={{}} summary={{ trained: false }} />);

    expect(screen.queryByText(/more closed signals/i)).toBeNull();
  });
});
