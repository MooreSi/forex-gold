import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ScheduleWindowRow } from "../internal/ScheduleWindowRow";

/**
 * The Trend PA engine has its own toggle and strategy override in every
 * Trading Schedule window, like Reversal and Breakout (owner, 2026-09-29).
 * The keys are the ones `risk/schedule.py` stores: trend_pa_engine and
 * trend_pa_engine_override.
 */
const block = {
  enabled: true, start: "08:00", end: "21:00", target: 0,
  reversal_engine: true, breakout_engine: true, trend_pa_engine: true,
  trend_pa_engine_override: "", telegram_default_enabled: true, telegram_channels: {},
};

async function open(patch = vi.fn(), over: Record<string, unknown> = {}) {
  render(<ScheduleWindowRow day="monday" dayLabel="Monday" index={0}
    block={{ ...block, ...over }} channels={[]}
    options={[{ value: "", label: "—" }, { value: "be_runner", label: "BE Runner" }]}
    onPatch={patch} />);
  await userEvent.click(screen.getByRole("button", { name: /channels/i }));
  return patch;
}

describe("Trend PA in a schedule window", () => {
  it("can be blocked for the window", async () => {
    const patch = await open();
    await userEvent.click(screen.getByLabelText("Trend PA Engine in Monday window 1"));
    expect(patch).toHaveBeenCalledWith({ trend_pa_engine: false });
  });

  it("reads a stored block as blocked", async () => {
    await open(vi.fn(), { trend_pa_engine: false });
    expect(screen.getByLabelText("Trend PA Engine in Monday window 1")).not.toBeChecked();
  });

  it("has its own strategy override", async () => {
    const patch = await open();
    await userEvent.selectOptions(
      screen.getByLabelText("Trend PA Engine override in Monday window 1"), "be_runner");
    expect(patch).toHaveBeenCalledWith({ trend_pa_engine_override: "be_runner" });
  });

  it("is counted among the window's sources", async () => {
    await open();
    expect(screen.getByRole("button", { name: /channels/i })).toHaveTextContent("3/3");
  });
});
