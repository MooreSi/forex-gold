import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { BreakoutActivity } from "../internal/BreakoutActivity";

/**
 * What the engine has been doing, and why it has not.
 *
 * The last three of `panel_data`'s seventeen operations with no caller. The
 * analysis log is the one that earns its place: every other panel reports
 * what an engine DID, and a suppressed candidate leaves no trade behind — so
 * without it there is no way to tell a quiet market from a gate set too
 * tight.
 */
const SIGNALS = [
  { id: 128, signal_ref: "BO-324FEF14", created_at: 1789709930, direction: "BUY",
    breakout_type: "sweep", outcome: "loss", net_pnl_dollars: -41.2 },
];

const LOG = [
  { id: 82161, ts: 1789848014, result: "closed",
    suppressed_reason: "Market closed (weekend)" },
  { id: 82160, ts: 1789848000, result: "passed", suppressed_reason: "" },
];

const PARAMS = {
  min_adx_go: { value: 30.0, default: 28.0, min: 20, max: 45, desc: "Minimum ADX" },
  min_adx_retest: { value: 24.0, default: 24.0, min: 18, max: 40, desc: "Retest ADX" },
};

const render_ = (over: Record<string, unknown> = {}) =>
  render(<BreakoutActivity signals={SIGNALS} log={LOG} params={PARAMS} {...over} />);

describe("it stays out of the way", () => {
  it("is collapsed until asked for", async () => {
    // Detail for a question, not a dashboard.
    render_();

    expect(screen.queryByTestId("bo-log")).not.toBeInTheDocument();
  });

  it("says how much is behind each one without opening it", async () => {
    render_();

    expect(screen.getByTestId("bo-log-toggle")).toHaveTextContent("2");
    expect(screen.getByTestId("bo-signals-toggle")).toHaveTextContent("1");
  });

  it("opens when asked", async () => {
    render_();

    await userEvent.click(screen.getByTestId("bo-log-toggle"));

    expect(screen.getByTestId("bo-log")).toBeInTheDocument();
  });
});

describe("why it did not act", () => {
  it("shows the reason, not the verdict", async () => {
    // "suppressed" on its own is the thing this panel exists to replace.
    render_();
    await userEvent.click(screen.getByTestId("bo-log-toggle"));

    expect(screen.getByTestId("bo-log-82161"))
      .toHaveTextContent("Market closed (weekend)");
  });

  it("falls back to the result when there is no reason", async () => {
    render_();
    await userEvent.click(screen.getByTestId("bo-log-toggle"));

    expect(screen.getByTestId("bo-log-82160")).toHaveTextContent("passed");
  });
});

describe("the signals", () => {
  it("shows what each one was and what it made", async () => {
    render_();
    await userEvent.click(screen.getByTestId("bo-signals-toggle"));

    const row = screen.getByTestId("bo-signal-128");
    expect(row).toHaveTextContent("BUY");
    expect(row).toHaveTextContent("sweep");
    expect(row).toHaveTextContent("-$41.20");
  });
});

describe("the self-tuned parameters", () => {
  it("shows a drifted value against the default it moved from", async () => {
    // A threshold that has drifted is the engine telling you something, and
    // a number with nothing to compare it to is not.
    render_();
    await userEvent.click(screen.getByTestId("bo-params-toggle"));

    expect(screen.getByTestId("bo-param-min_adx_go")).toHaveTextContent("was 28");
  });

  it("says a parameter still on its default is on its default", async () => {
    render_();
    await userEvent.click(screen.getByTestId("bo-params-toggle"));

    expect(screen.getByTestId("bo-param-min_adx_retest")).toHaveTextContent("default");
  });

  it("marks a drifted value so it can be found at a glance", async () => {
    render_();
    await userEvent.click(screen.getByTestId("bo-params-toggle"));

    const cells = within(screen.getByTestId("bo-param-min_adx_go")).getAllByRole("cell");
    expect(cells[1]!.className).toContain("text-accent");
  });
});

describe("when there is nothing", () => {
  it("says so rather than drawing an empty table", async () => {
    render_({ signals: [], log: [], params: {} });
    await userEvent.click(screen.getByTestId("bo-log-toggle"));

    expect(screen.getByTestId("bo-log")).toHaveTextContent("nothing recorded yet");
  });

  it("survives payloads that are not the shape it expects", async () => {
    render_({ signals: "no", log: null, params: "nope" });

    expect(screen.getByTestId("bo-log-toggle")).toHaveTextContent("0");
  });
});
