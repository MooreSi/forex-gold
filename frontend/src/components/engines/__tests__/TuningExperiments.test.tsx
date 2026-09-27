/**
 * The Breakout tuning experiments card (docs/todo/007).
 *
 * The backend decides and judges; the card renders what it said and sends the
 * owner's decisions. Two things it must get right on its own: Approve asks
 * for confirmation before changing a live engine, and it cannot start a second
 * experiment while one is running.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  TuningExperimentsView, type Experiment, type TuningState,
} from "../internal/TuningExperimentsSection";

function exp(over: Partial<Experiment> = {}): Experiment {
  return {
    id: 1, param: "min_adx_go", old_value: 28, new_value: 32,
    hypothesis: "late trends lose", summary: "", status: "proposed",
    concurrent: 1, baseline_n: null, baseline_mean: null,
    after_n: null, after_mean: null, after_sum: null, verdict: null, ...over,
  };
}

function state(over: Partial<TuningState> = {}): TuningState {
  return {
    approval_required: true, min_sample: 30, failure_usd: 100,
    running: null, proposals: [exp()], history: [], ...over,
  };
}

function view(s: TuningState) {
  const cb = { onApprove: vi.fn(), onReject: vi.fn(), onSetApproval: vi.fn() };
  render(<TuningExperimentsView state={s} {...cb} />);
  return cb;
}

describe("TuningExperimentsView", () => {
  it("approve asks first and only then sends", async () => {
    const cb = view(state());
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(cb.onApprove).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(cb.onApprove).toHaveBeenCalledWith(1);
  });

  it("cancel sends nothing", async () => {
    const cb = view(state());
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(cb.onApprove).not.toHaveBeenCalled();
  });

  it("cannot approve a second change while one is running", () => {
    view(state({
      running: exp({ id: 9, status: "running", after_n: 4, after_mean: 2, baseline_mean: 5 }),
      proposals: [exp({ id: 2 })],
    }));
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByTestId("tuning-running")).toHaveTextContent("4/30");
  });

  it("reject sends the id", async () => {
    const cb = view(state());
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(cb.onReject).toHaveBeenCalledWith(1);
  });

  it("the approval switch reports its new value", async () => {
    const cb = view(state({ approval_required: false }));
    await userEvent.click(screen.getByLabelText("Require my approval"));
    expect(cb.onSetApproval).toHaveBeenCalledWith(true);
  });

  it("says when a change was one of several made together", () => {
    view(state({ proposals: [], history: [exp({ id: 5, status: "judged", concurrent: 3, verdict: "worse" })] }));
    expect(screen.getByTestId("tuning-history-5")).toHaveTextContent("1 of 3 changed together");
  });
});
