/**
 * The window the Dashboard's performance and equity cards are measured over.
 *
 * Asked for on 2026-10-09: 1 day, 1 week, 30 days or all time, chosen on the
 * Trading performance card, moving the Equity curve card with it, and still
 * chosen after a reload. On 2026-10-10 "1 day" became Today: from midnight on
 * the trading clock, not the last 24 hours (a Saturday read -$44.22 of
 * Friday's trading).
 *
 * What is pinned is what the cards ASK FOR, not only what they say: a label
 * that reads "last 7 days" over a 30-day request is the failure this exists
 * to prevent, and it renders perfectly well.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardPanel } from "../DashboardPanel";
import { WINDOW_KEY } from "../hooks/useDashboardWindow";
import { resetPolls } from "@/hooks/usePoll";

vi.mock("lightweight-charts", async () => (await import("@/test/chartStub")).chartStub());

let fetchMock: ReturnType<typeof vi.fn>;

const HISTORY = {
  days: 30,
  performance: {
    balance: 10_000, closed_trades: 4, win_rate_pct: 50,
    total_net_pnl: 12.5, profit_factor: 1.2, max_drawdown_pct: 1.1,
  },
  hourly: [], channels: [], ladder: {},
};

const CLOSED = {
  rows: [], error: null,
  curve: {
    points: [{ ts: 1, pnl: 5 }, { ts: 2, pnl: 12.5 }],
    net: 12.5, peak: 12.5, max_drawdown: 0, trades: 4,
  },
};

function jsonFor(url: string): unknown {
  if (url.startsWith("/api/history/state")) return HISTORY;
  if (url.startsWith("/api/history/trades")) return CLOSED;
  if (url.startsWith("/api/ai/research")) return { analysis: null, saved_at: "" };
  if (url.startsWith("/api/chart/candles")) return [];
  if (url.startsWith("/api/trading/trades") || url.startsWith("/api/chart/trades")) return [];
  return {};
}

/** The `days` every GET to `prefix` has asked for, in order. */
function daysAskedOf(prefix: string): number[] {
  return fetchMock.mock.calls
    .map((c) => String(c[0]))
    .filter((u) => u.startsWith(prefix))
    .map((u) => Number(new URL(u, "http://x").searchParams.get("days")));
}

/** Whether each GET to `prefix` asked for today on the trading clock. */
function todayAskedOf(prefix: string): boolean[] {
  return fetchMock.mock.calls
    .map((c) => String(c[0]))
    .filter((u) => u.startsWith(prefix))
    .map((u) => new URL(u, "http://x").searchParams.get("today") === "true");
}

/** This repository's jsdom provides no `localStorage` at all. */
function fakeStorage(): Storage {
  const held = new Map<string, string>();
  return {
    getItem: (k: string) => held.get(k) ?? null,
    setItem: (k: string, v: string) => { held.set(k, v); },
    removeItem: (k: string) => { held.delete(k); },
    clear: () => held.clear(),
    key: () => null,
    get length() { return held.size; },
  } as Storage;
}

function windowButton(name: string): HTMLElement {
  return screen.getByRole("button", { name });
}

beforeEach(() => {
  resetPolls();
  vi.stubGlobal("localStorage", fakeStorage());
  fetchMock = vi.fn(async (url: string) =>
    ({ ok: true, status: 200, json: async () => jsonFor(url) }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("the Dashboard's performance window", () => {
  it("defaults to 30 days, which is what the screen showed before the choice existed", async () => {
    render(<DashboardPanel />);

    await waitFor(() => expect(daysAskedOf("/api/history/trades")).toContain(30));
    expect(daysAskedOf("/api/history/state")).toContain(30);
    expect(windowButton("30 days")).toHaveAttribute("aria-pressed", "true");
  });

  it("offers exactly Today, 1 week, 30 days and all time", async () => {
    render(<DashboardPanel />);

    for (const name of ["Today", "1 week", "30 days", "All time"]) {
      expect(windowButton(name)).toBeInTheDocument();
    }
  });

  it("re-asks both the performance figures and the equity curve for the chosen window", async () => {
    render(<DashboardPanel />);
    await waitFor(() => expect(daysAskedOf("/api/history/trades")).toContain(30));

    act(() => { windowButton("1 week").click(); });

    await waitFor(() => expect(daysAskedOf("/api/history/trades")).toContain(7));
    expect(daysAskedOf("/api/history/state")).toContain(7);
    expect(windowButton("1 week")).toHaveAttribute("aria-pressed", "true");
    expect(windowButton("30 days")).toHaveAttribute("aria-pressed", "false");
  });

  it("labels the equity curve with the window it was asked for", async () => {
    render(<DashboardPanel />);
    await screen.findByText("last 30 days");

    act(() => { windowButton("Today").click(); });

    expect(await screen.findByText("today")).toBeInTheDocument();
    expect(screen.queryByText("last 30 days")).toBeNull();
  });

  it("asks for the backend's ten-year ceiling for all time", async () => {
    // `/api/history/*` validates `days` as `le=3650` -- the router's own
    // comment says that is what the old "all" option meant. A larger number
    // is a 422 and the cards show "no broker data" for an account that has
    // plenty.
    render(<DashboardPanel />);

    act(() => { windowButton("All time").click(); });

    await waitFor(() => expect(daysAskedOf("/api/history/trades")).toContain(3650));
    expect(daysAskedOf("/api/history/state")).toContain(3650);
    expect(screen.getByText("all time")).toBeInTheDocument();
  });

  it("is still chosen after the page is reloaded", async () => {
    const first = render(<DashboardPanel />);
    act(() => { windowButton("1 week").click(); });
    first.unmount();
    resetPolls();
    fetchMock.mockClear();

    render(<DashboardPanel />);

    expect(windowButton("1 week")).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(daysAskedOf("/api/history/trades")).toContain(7));
    expect(daysAskedOf("/api/history/trades")).not.toContain(30);
  });

  it("falls back to 30 days when the stored value is not one it offers", async () => {
    // Somebody's hand-edited storage, or a window a later version dropped.
    // Asking the backend for `days=abc` is a 422, not a sensible default.
    localStorage.setItem(WINDOW_KEY, "abc");
    render(<DashboardPanel />);

    expect(windowButton("30 days")).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(daysAskedOf("/api/history/trades")).toContain(30));
  });

  it("still works when storage throws", async () => {
    // Private windows and blocked site data. Losing the preference is fine;
    // losing the Dashboard to an exception is not.
    const blocked = () => { throw new Error("blocked"); };
    vi.stubGlobal("localStorage", {
      getItem: blocked, setItem: blocked, removeItem: blocked, clear: blocked,
    });
    render(<DashboardPanel />);
    expect(windowButton("30 days")).toHaveAttribute("aria-pressed", "true");

    act(() => { windowButton("Today").click(); });

    await waitFor(() => expect(daysAskedOf("/api/history/trades")).toContain(1));
  });

  it("asks both cards for today on the trading clock, not the last 24 hours", async () => {
    render(<DashboardPanel />);
    await waitFor(() => expect(daysAskedOf("/api/history/trades")).toContain(30));
    expect(todayAskedOf("/api/history/trades")).not.toContain(true);

    act(() => { windowButton("Today").click(); });

    await waitFor(() => expect(todayAskedOf("/api/history/trades")).toContain(true));
    expect(todayAskedOf("/api/history/state")).toContain(true);
  });
});
