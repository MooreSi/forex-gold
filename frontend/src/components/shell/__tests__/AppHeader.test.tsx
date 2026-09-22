import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppHeader } from "../AppHeader";
import { AuthProvider } from "@/contexts/AuthContext";
import { resetPolls } from "@/hooks/usePoll";
import type { HeaderState } from "@/api/types";

/**
 * The bar on every tab, and the one thing it must not stay quiet about.
 *
 * Two independent things stop automated entries — the risk governor and the
 * circuit breaker — and they are stored under different keys. The header read
 * only the governor's between 2026-09-18 and the fix, so a tripped breaker
 * appeared nowhere but Settings > Diagnostics: an operator looking at this bar
 * would believe entries were running while they were being refused.
 *
 * The backend now decides both and hands over one `pause` object. This side
 * renders it; it does not re-derive anything.
 */
function header(over: Partial<HeaderState> = {}): HeaderState {
  return {
    account: { is_demo: true, login: 5203117 },
    bridge: { connected: true },
    tick: null,
    active_trader: "local",
    pause: { paused: false, reason: "", until: null, source: "" },
    remote_connected: false,
    ea_badge: { colour: "green", text: "EA", tooltip: "healthy", stale: false, scope: "global" },
    ...over,
  } as HeaderState;
}

let body: HeaderState;
/** What `/api/trading/status-badge` answers. The header's single halt
 *  indicator reads this endpoint, not `/api/system/header`. */
let badge: Record<string, unknown>;

beforeEach(() => {
  resetPolls();
  body = header();
  badge = {
    state: "ok", label: "Trading Active",
    detail: "Nothing is holding automated entries.",
    until: null, resume_ts: null, can_resume: false,
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
    ok: true, status: 200,
    json: async () => (String(url).includes("status-badge") ? badge : body),
  })));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const renderHeader = () => render(<AuthProvider><AppHeader /></AuthProvider>);

describe("the halt, stated once", () => {
  /**
   * It used to be stated three times: a badge of its own in the middle of the
   * bar, the trading-status badge on the right, and the Pause button growing
   * into a "Paused" pill. All three on a header that already runs out of room
   * at 1024px, and the owner reported the congestion on 2026-09-21.
   *
   * There is one now, and it is the one that knows most: `TradingStatusBadge`
   * reads the backend's own decision across all FOUR mechanisms that can hold
   * an entry, where the removed badge knew about two. Clicking it offers the
   * Resume.
   */
  it("says so when nothing is stopped", async () => {
    renderHeader();

    expect(await screen.findByTestId("trading-status-badge"))
      .toHaveTextContent("Trading Active");
  });

  it("states the halt exactly once", async () => {
    // The whole point of the change. Two elements saying "paused" on one bar
    // is what made the third unreadable.
    badge = {
      state: "halted", label: "Trading Paused until 21 Sep 18:00",
      detail: "daily loss limit reached", until: 1_800_000_000,
      resume_ts: null, can_resume: true,
    };
    body = header({
      pause: { paused: true, reason: "daily loss limit reached",
               until: 1_800_000_000, source: "governor" },
    });
    renderHeader();
    await screen.findByTestId("trading-status-badge");

    expect(screen.queryByTestId("pause-badge")).not.toBeInTheDocument();
    expect(screen.getAllByText(/paused/i).length).toBe(1);
  });

  it("shows a governor halt with its reason", async () => {
    badge = {
      state: "halted", label: "Trading Paused until 21 Sep 18:00",
      detail: "daily loss limit reached", until: 1_800_000_000,
      resume_ts: null, can_resume: true,
    };
    renderHeader();

    expect(await screen.findByTestId("trading-status-badge"))
      .toHaveAttribute("title", "daily loss limit reached");
  });

  it("shows a tripped circuit breaker, which used to be invisible here", async () => {
    badge = {
      state: "halted", label: "Trading Paused until 21 Sep 18:00",
      detail: "Circuit breaker active (3 consecutive losses)",
      until: 1_800_000_000, resume_ts: null, can_resume: true,
    };
    renderHeader();

    expect(await screen.findByTestId("trading-status-badge"))
      .toHaveAttribute("title", expect.stringContaining("3 consecutive losses"));
  });

  it("says when trading resumes", async () => {
    // "Halted" with no resume time leaves the operator watching the screen to
    // find out when it lifts.
    badge = {
      state: "halted", label: "Trading Paused until 21 Sep 18:00",
      detail: "drawdown", until: 1_800_000_000, resume_ts: null, can_resume: true,
    };
    renderHeader();

    expect(await screen.findByTestId("trading-status-badge"))
      .toHaveTextContent(/until \d\d \w\w\w \d\d:\d\d/);
  });

  it("reports the two mechanisms the removed badge could not see", async () => {
    // A profit target reached and a news blackout both hold every automated
    // entry, and neither writes the key `pause` read. A header that said
    // nothing about them was a false all-clear.
    badge = {
      state: "profit_target", label: "Profit Target Reached",
      detail: "Daily profit target reached ($120.00 of $100.00)",
      until: null, resume_ts: null, can_resume: true,
    };
    renderHeader();

    expect(await screen.findByTestId("trading-status-badge"))
      .toHaveTextContent("Profit Target Reached");
  });

  it("renders whatever the backend decided, without re-deciding it", async () => {
    // Negative control for the whole file: a header that applied its own rule
    // for "is this really a halt" would disagree with the engines.
    badge = {
      state: "halted", label: "Trading Paused until 21 Sep 18:00",
      detail: "", until: null, resume_ts: null, can_resume: true,
    };
    renderHeader();

    expect(await screen.findByTestId("trading-status-badge"))
      .toHaveTextContent("Trading Paused");
  });

  it("is the control that lifts it, not just a label", async () => {
    badge = {
      state: "halted", label: "Trading Paused until 21 Sep 18:00",
      detail: "daily loss limit reached", until: 1_800_000_000,
      resume_ts: null, can_resume: true,
    };
    renderHeader();

    await userEvent.click(await screen.findByTestId("trading-status-badge"));

    expect(screen.getByTestId("trading-status-dialog")).toBeInTheDocument();
  });
});

describe("pausing and resuming, from the one control", () => {
  /**
   * Restored 2026-09-18 as a Pause button of its own. Merged into the status
   * badge on 2026-09-22: two controls for one fact was congestion on a bar
   * that runs out of room at 1024px, and the Pause button read only the
   * governor's manual pause — with a profit target reached it offered "Pause"
   * while every automated entry was already being held.
   *
   * The badge reads the backend's decision across all four mechanisms, so it
   * is the one that knows which half to offer.
   */
  const writes = () =>
    (global.fetch as ReturnType<typeof vi.fn>).mock.calls
      .filter((c) => c[1]?.method === "POST");

  const HALTED = {
    state: "halted", label: "Trading Paused until 21 Sep 18:00",
    detail: "manually paused", until: 1_800_000_000,
    resume_ts: null, can_resume: true,
  };

  const openStatus = async () =>
    userEvent.click(await screen.findByTestId("trading-status-badge"));

  it("is the only pause control on the bar", async () => {
    renderHeader();
    await screen.findByTestId("trading-status-badge");

    // The removed button. Its absence is the change; a second control that
    // knows less than this one is how the "Pause" offer appeared during a
    // profit-target hold.
    expect(screen.queryByTestId("pause-control")).not.toBeInTheDocument();
  });

  it("offers Pause when trading is running", async () => {
    renderHeader();
    await openStatus();

    expect(await screen.findByRole("button", { name: "Pause now" })).toBeInTheDocument();
  });

  it("offers Resume when it is paused", async () => {
    badge = HALTED;
    renderHeader();
    await openStatus();

    expect(await screen.findByRole("button", { name: "Resume Trading" })).toBeInTheDocument();
  });

  it("offers Resume — not Pause — when the profit target is what holds entries", async () => {
    // The bug the merge fixes. The old Pause button read `pause.paused`,
    // which a daily target does not set, so it offered to pause something
    // already stopped and the way to lift it was elsewhere.
    badge = {
      state: "profit_target", label: "Profit Target Reached",
      detail: "Daily profit target reached ($120.00 of $100.00)",
      until: null, resume_ts: null, can_resume: true,
    };
    renderHeader();
    await openStatus();

    expect(await screen.findByRole("button", { name: "Resume Trading" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause now" })).not.toBeInTheDocument();
  });

  it("does not pause on the first press", async () => {
    renderHeader();

    await openStatus();

    expect(writes()).toHaveLength(0);
  });

  it("says what a pause does NOT stop, which is the part people get wrong", async () => {
    renderHeader();
    await openStatus();

    expect(screen.getByText(/SL\/TP monitoring\) continues as normal/)).toBeInTheDocument();
    expect(screen.getByText(/generators and Telegram signals continue/)).toBeInTheDocument();
  });

  it("pauses for the hours given", async () => {
    renderHeader();
    await openStatus();

    const hours = screen.getByLabelText("Pause for (hours)");
    await userEvent.clear(hours);
    await userEvent.type(hours, "2");
    await userEvent.click(screen.getByRole("button", { name: "Pause now" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/trading/pause");
    expect(JSON.parse(writes()[0][1].body)).toEqual({ hours: 2 });
  });

  it("a typed moment wins over the hours box", async () => {
    renderHeader();
    await openStatus();

    await userEvent.type(
      screen.getByLabelText("Or until (YYYY-MM-DD HH:MM)"), "2030-01-02 09:30");
    await userEvent.click(screen.getByRole("button", { name: "Pause now" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    const sent = JSON.parse(writes()[0][1].body);
    expect(sent.hours).toBeUndefined();
    expect(sent.until).toBeGreaterThan(1_700_000_000);
  });

  it("refuses an unparseable moment rather than silently pausing for 4 hours", async () => {
    // Falling back to the hours default here would pause for a length the
    // operator never asked for and did not see.
    renderHeader();
    await openStatus();

    await userEvent.type(
      screen.getByLabelText("Or until (YYYY-MM-DD HH:MM)"), "next tuesday");
    await userEvent.click(screen.getByRole("button", { name: "Pause now" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("not a date and time");
    expect(writes()).toHaveLength(0);
  });

  it("resuming says the guards are re-armed, and asks the backend to do it", async () => {
    badge = HALTED;
    renderHeader();
    await openStatus();

    expect(screen.getByText(/restarts the post-close guards/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Resume Trading" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    // `resume-all` since 2026-09-21: it re-arms the post-close guards exactly
    // as `resume` did (same `manual_pause.resume()`), and also clears a
    // tripped breaker or a daily halt. With either of those in force the old
    // route left the button doing nothing visible.
    expect(writes()[0][0]).toBe("/api/trading/resume-all");
  });
});
