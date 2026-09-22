/**
 * One control for "is trading running, and change it".
 *
 * Until 2026-09-22 the header carried two: a Pause button that opened a
 * pause/resume dialog, and a status badge that opened a resume-only popover.
 * Two controls for one fact on a bar that already runs out of room at 1024px,
 * and neither one told the whole story — the Pause button read only the
 * governor's manual pause, so with a profit target reached it offered "Pause"
 * while every automated entry was already being held.
 *
 * The badge reads the backend's decision across all four mechanisms. It is now
 * the only control: clicking it opens this dialog, which offers a Resume when
 * one would do something and the pause form when nothing is holding entries.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TradingStatusDialog } from "../TradingStatusDialog";
import type { TradingStatus } from "../TradingStatusDialog";

const OK: TradingStatus = {
  state: "ok", label: "Trading Active",
  detail: "Nothing is holding automated entries.",
  until: null, resume_ts: null, can_resume: false,
};

const PROFIT_TARGET: TradingStatus = {
  state: "profit_target", label: "Profit Target Reached",
  detail: "Daily profit target reached ($120.00 of $100.00) — automated "
        + "entries are held for the rest of today.",
  until: null, resume_ts: null, can_resume: true,
};

let posts: Array<[string, unknown]>;

beforeEach(() => {
  posts = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push([String(url), init.body ? JSON.parse(String(init.body)) : null]);
    }
    return { ok: true, status: 200, json: async () => ({}) };
  }));
});
afterEach(() => vi.unstubAllGlobals());

const show = (status: TradingStatus, onChanged = () => {}) =>
  render(
    <TradingStatusDialog
      open
      onOpenChange={() => {}}
      status={status}
      onChanged={onChanged}
    />,
  );

describe("when a hold is in force", () => {
  it("offers to resume, and says what the state actually is", async () => {
    show(PROFIT_TARGET);

    expect(await screen.findByText(/Daily profit target reached/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume Trading" })).toBeInTheDocument();
  });

  it("resumes through the route that clears every hold", async () => {
    // Three mechanisms stop new orders and `/api/trading/resume` lifts only
    // the manual pause. With a profit target in force that route leaves the
    // button doing nothing visible (owner report, 2026-09-21).
    show(PROFIT_TARGET);

    await userEvent.click(screen.getByRole("button", { name: "Resume Trading" }));

    await waitFor(() => expect(posts.map((p) => p[0])).toEqual(["/api/trading/resume-all"]));
  });

  it("does not resume on opening — the click that opened this is not consent", async () => {
    show(PROFIT_TARGET);

    expect(posts).toHaveLength(0);
  });

  it("does not offer a pause while entries are already held", () => {
    show(PROFIT_TARGET);

    expect(screen.queryByRole("button", { name: "Pause now" })).not.toBeInTheDocument();
  });

  it("says the post-close guards are re-armed", async () => {
    // Without that, a resume after a give-back halt lasts exactly until the
    // next trade closes.
    show(PROFIT_TARGET);

    expect(await screen.findByText(/restarts the post-close guards/)).toBeInTheDocument();
  });
});

describe("when nothing is holding entries", () => {
  it("offers the pause form instead of a resume", async () => {
    show(OK);

    expect(await screen.findByLabelText("Pause for (hours)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume Trading" })).not.toBeInTheDocument();
  });

  it("says what a pause does NOT stop, which is the part people get wrong", () => {
    show(OK);

    expect(screen.getByText(/generators and Telegram signals continue/)).toBeInTheDocument();
    expect(screen.getByText(/SL\/TP monitoring\) continues as normal/)).toBeInTheDocument();
  });

  it("pauses for the hours given", async () => {
    show(OK);

    const hours = screen.getByLabelText("Pause for (hours)");
    await userEvent.clear(hours);
    await userEvent.type(hours, "2");
    await userEvent.click(screen.getByRole("button", { name: "Pause now" }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0][0]).toBe("/api/trading/pause");
    expect(posts[0][1]).toEqual({ hours: 2 });
  });

  it("a typed moment wins over the hours box", async () => {
    show(OK);

    await userEvent.type(
      screen.getByLabelText("Or until (YYYY-MM-DD HH:MM)"), "2030-01-02 09:30");
    await userEvent.click(screen.getByRole("button", { name: "Pause now" }));

    await waitFor(() => expect(posts).toHaveLength(1));
    const sent = posts[0][1] as { hours?: number; until?: number };
    expect(sent.hours).toBeUndefined();
    expect(sent.until).toBeGreaterThan(1_700_000_000);
  });

  it("refuses an unparseable moment rather than silently pausing for 4 hours", async () => {
    show(OK);

    await userEvent.type(
      screen.getByLabelText("Or until (YYYY-MM-DD HH:MM)"), "next tuesday");
    await userEvent.click(screen.getByRole("button", { name: "Pause now" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("not a date and time");
    expect(posts).toHaveLength(0);
  });

  it("lets an operator pause during a news blackout, which it cannot resume", async () => {
    // The blackout lifts itself, so there is no Resume. That must not cost the
    // operator the ability to stop trading by hand.
    show({
      state: "news_blackout", label: "News Blackout",
      detail: "Non-farm payrolls", until: null,
      resume_ts: null, can_resume: false,
    });

    expect(await screen.findByRole("button", { name: "Pause now" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume Trading" })).not.toBeInTheDocument();
  });
});
