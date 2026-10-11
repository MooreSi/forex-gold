/**
 * A reached daily goal is not lifted by Resume (owner, 2026-10-10).
 *
 * On 9 October a goal secured at 05:46 was resumed past at 07:30 and the day
 * traded on to -$39.28. The backend now leaves the goal's halt in place and
 * the badge says `can_resume: false`. The dialog must then offer neither a
 * Resume (it would do nothing) nor the Pause form (a shorter pause written
 * over the goal's would end it early): only the reason and a way out.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TradingStatusDialog } from "../TradingStatusDialog";
import type { TradingStatus } from "../TradingStatusDialog";

const GOAL: TradingStatus = {
  state: "halted",
  label: "Goal Achieved Paused until 10 Oct 00:00",
  detail: "Daily goal secured: +$29.57 today vs a goal of $22.11 / Today's "
        + "daily goal is reached, so trading stays paused until midnight. To "
        + "trade on today, raise the goal or switch it off in Risk settings.",
  until: 1_791_586_800, resume_ts: null, can_resume: false,
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () =>
    ({ ok: true, status: 200, json: async () => ({}) })));
});
afterEach(() => vi.unstubAllGlobals());

it("shows the goal and what lifts it, with no Resume and no Pause", () => {
  render(<TradingStatusDialog open onOpenChange={() => {}} status={GOAL}
                              onChanged={() => {}} />);

  expect(screen.getByText(GOAL.label)).toBeInTheDocument();
  expect(screen.getByText(/switch it off in Risk settings/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Resume/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /Pause/ })).toBeNull();
  expect(screen.queryByLabelText("Pause for (hours)")).toBeNull();
  expect(screen.getAllByRole("button", { name: "Close" }).length).toBeGreaterThan(0);
});
