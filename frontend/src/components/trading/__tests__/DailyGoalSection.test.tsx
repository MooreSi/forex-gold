import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DailyGoalSection } from "../internal/DailyGoalSection";

/**
 * Risk > Stopping for the day > Daily goal (owner, 2026-09-29,
 * docs/todo/risk/020): a % or $ amount to secure for the day. Once today's
 * realised profit reaches it, new entries stop until the next broker day.
 *
 * Every write names a real `vantage_risk_settings` column (migration 55):
 * daily_goal_enabled, daily_goal_mode, daily_goal_value.
 */
const base = { daily_goal_enabled: 1, daily_goal_mode: "pct", daily_goal_value: 1.5 };

function renderWith(data: Record<string, unknown>) {
  const save = vi.fn(async () => {});
  render(<DailyGoalSection data={data} version={1} save={save} />);
  return save;
}

describe("daily goal", () => {
  it("is off until switched on, and says the rest does nothing", () => {
    renderWith({ ...base, daily_goal_enabled: 0 });
    expect(screen.getByLabelText("Stop for the day once the goal is secured")).not.toBeChecked();
    expect(screen.getByTestId("daily-goal-summary")).toHaveTextContent(/off/i);
  });

  it("switching it on writes the 0/1 the column holds", async () => {
    const save = renderWith({ ...base, daily_goal_enabled: 0 });
    await userEvent.click(screen.getByLabelText("Stop for the day once the goal is secured"));
    expect(save).toHaveBeenCalledWith({ daily_goal_enabled: 1 });
  });

  it("shows the stored percentage on the slider and in words", () => {
    renderWith(base);
    expect(screen.getByRole("slider", { name: /daily goal/i })).toHaveValue("1.5");
    expect(screen.getByTestId("daily-goal-summary")).toHaveTextContent(
      "1.5% of the day's opening balance");
  });

  it("choosing $ writes the mode, and a sensible dollar amount", async () => {
    const save = renderWith(base);
    await userEvent.click(screen.getByRole("radio", { name: "$" }));
    expect(save).toHaveBeenCalledWith({ daily_goal_mode: "usd", daily_goal_value: 100 });
  });

  it("choosing the mode already in force writes nothing", async () => {
    const save = renderWith(base);
    await userEvent.click(screen.getByRole("radio", { name: "%" }));
    expect(save).not.toHaveBeenCalled();
  });

  it("a slider move is saved once, on release", () => {
    const save = renderWith(base);
    const slider = screen.getByRole("slider", { name: /daily goal/i });
    fireEvent.change(slider, { target: { value: "2.5" } });
    expect(save).not.toHaveBeenCalled();
    fireEvent.pointerUp(slider);
    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith({ daily_goal_value: 2.5 });
  });

  it("the dollar mode reads in dollars", () => {
    renderWith({ ...base, daily_goal_mode: "usd", daily_goal_value: 250 });
    expect(screen.getByTestId("daily-goal-summary")).toHaveTextContent("$250");
    expect(screen.getByRole("slider", { name: /daily goal/i })).toHaveValue("250");
  });

  it("has a test id per column for the Risk tab's coverage check", () => {
    renderWith(base);
    for (const key of ["daily_goal_enabled", "daily_goal_mode", "daily_goal_value"]) {
      expect(screen.getByTestId(`risk-${key}`)).toBeInTheDocument();
    }
  });

  it("has a breakeven tickbox beside the mode toggle, off by default", () => {
    renderWith(base);
    expect(screen.getByLabelText("Move stops to breakeven once reached")).not.toBeChecked();
  });

  it("ticking it writes the 0/1 the column holds (migration 56)", async () => {
    const save = renderWith(base);
    await userEvent.click(screen.getByLabelText("Move stops to breakeven once reached"));
    expect(save).toHaveBeenCalledWith({ daily_goal_protect_be: 1 });
  });

  it("shows it ticked when stored on, and unticking writes 0", async () => {
    const save = renderWith({ ...base, daily_goal_protect_be: 1 });
    const box = screen.getByLabelText("Move stops to breakeven once reached");
    expect(box).toBeChecked();
    await userEvent.click(box);
    expect(save).toHaveBeenCalledWith({ daily_goal_protect_be: 0 });
  });
});
