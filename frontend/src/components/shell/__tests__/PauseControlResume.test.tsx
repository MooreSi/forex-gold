/**
 * Resume trading must clear whatever is actually holding entries.
 *
 * Reported on 2026-09-21: "when the trading is paused and i click the pause
 * button and then the resume trading button it doesn't resume trading".
 *
 * There are three separate mechanisms that stop new orders -- the governor's
 * manual pause, the circuit breaker, and the daily profit/loss halt -- and
 * this button posted to `/api/trading/resume`, which lifts the FIRST one only.
 * When the hold in force was either of the others, the button did nothing
 * visible. `trading_status.resume_all`'s own docstring predicted this exactly:
 * "a Resume that only cleared the governor would leave the operator pressing a
 * button that visibly does nothing while the breaker is the thing holding
 * entries".
 *
 * `/api/trading/resume-all` clears whichever is in force, manual pause
 * included, and is what the status badge's own Resume already used.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PauseControl } from "../PauseControl";

let posts: string[];

beforeEach(() => {
  posts = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") posts.push(String(url));
    return { ok: true, status: 200, json: async () => ({ paused: false, until: null }) };
  }));
});
afterEach(() => vi.unstubAllGlobals());

async function openResume() {
  render(<PauseControl paused onChanged={() => {}} />);
  await userEvent.click(screen.getByRole("button", { name: /paused/i }));
  return screen.getByRole("button", { name: /resume trading/i });
}

describe("resuming", () => {
  it("clears every hold, not just the manual pause", async () => {
    const button = await openResume();

    await userEvent.click(button);

    await waitFor(() => expect(posts).toEqual(["/api/trading/resume-all"]));
  });

  it("does not post to the governor-only route", async () => {
    // The bug: that route leaves a tripped breaker or a daily halt in place.
    const button = await openResume();

    await userEvent.click(button);

    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts).not.toContain("/api/trading/resume");
  });
});
