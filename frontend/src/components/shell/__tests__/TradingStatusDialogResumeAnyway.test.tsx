/**
 * "Resume anyway (ignore today's goal)" (owner, 2026-10-16).
 *
 * Plain Resume still leaves a reached goal's halt in place (2026-10-10). This
 * is a second, explicitly worded button, offered only when the backend says
 * the halt is the goal's (`can_resume_past_goal`, or `_on_vps` for a badge
 * that reports the VPS). It posts to its own route, never `resume-all`, and
 * the VPS's goes to the VPS, never this node.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TradingStatusDialog } from "../TradingStatusDialog";
import type { TradingStatus } from "../TradingStatusDialog";

const GOAL: TradingStatus = {
  state: "halted",
  label: "Goal Achieved Paused until 17 Oct 00:00",
  detail: "Daily goal secured: +$29.57 today vs a goal of $22.11",
  until: 1_791_586_800, resume_ts: null, can_resume: false,
  can_resume_past_goal: true,
};

const VPS_GOAL: TradingStatus = {
  ...GOAL,
  label: "VPS: Goal Achieved Paused until 17 Oct 00:00",
  can_resume_past_goal: undefined,
  node: "vps", can_resume_on_vps: false, can_resume_past_goal_on_vps: true,
};

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const show = (status: TradingStatus, onChanged = () => {}) =>
  render(<TradingStatusDialog open onOpenChange={() => {}} status={status}
                              onChanged={onChanged} />);

const ANYWAY = { name: "Resume anyway (ignore today's goal)" };

it("offers the override over the goal's own halt and posts to its own route", async () => {
  const changed = vi.fn();
  show(GOAL, changed);

  fireEvent.click(screen.getByRole("button", ANYWAY));

  await waitFor(() => expect(changed).toHaveBeenCalled());
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/api\/trading\/resume-past-goal$/);
});

it("is not offered when the backend does not say the halt is the goal's", () => {
  show({ ...GOAL, can_resume_past_goal: undefined });

  expect(screen.queryByRole("button", ANYWAY)).toBeNull();
});

it("still offers no plain Resume and no Pause form over the goal", () => {
  show(GOAL);

  expect(screen.queryByRole("button", { name: "Resume Trading" })).toBeNull();
  expect(screen.queryByLabelText("Pause for (hours)")).toBeNull();
});

it("sends the VPS's override to the VPS", async () => {
  const changed = vi.fn();
  show(VPS_GOAL, changed);

  fireEvent.click(screen.getByRole("button", ANYWAY));

  await waitFor(() => expect(changed).toHaveBeenCalled());
  expect(String(fetchMock.mock.calls[0][0]))
    .toMatch(/\/api\/trading\/remote\/resume-past-goal$/);
});

it("is not offered for a VPS that does not say so", () => {
  show({ ...VPS_GOAL, can_resume_past_goal_on_vps: false });

  expect(screen.queryByRole("button", ANYWAY)).toBeNull();
});
