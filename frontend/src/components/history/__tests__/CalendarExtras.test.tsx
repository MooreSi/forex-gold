import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CalendarSection } from "../internal/CalendarSection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The Calendar's journal features (docs/todo/023), wired together: the metric
 * toggle, goal and news markers, the day's stats, the journal and the year
 * view. Their arithmetic is in calendarStats.test.ts; this is what reaches the
 * screen, and which request carries what.
 */
function trade(over: Record<string, unknown> = {}) {
  return {
    ticket: 1, direction: "BUY", entry_price: 2400, exit_price: 2410,
    open_ts: 0, close_ts: Date.UTC(2026, 8, 15, 15, 0) / 1000, day: "2026-09-15",
    lots: 0.1, close_lots: [0.1], pnl: 50, fees: 0, pips: 10, duration_secs: null,
    order_type: "Market", pending_secs: null, reason: "TP hit",
    source: "GoldSignals", strategy: "", max_tp: "", rr: 1.25,
    spread_points: null, group: null, session: "london", ...over,
  };
}

let rows: Record<string, unknown>[];
let risk: Record<string, unknown>;
let news: Record<string, unknown>[];
let entries: Record<string, unknown>[];
let calls: { url: string; method: string; body?: unknown }[];

/** This repository's jsdom provides no `localStorage` at all. */
function fakeStorage(): Storage {
  const m = new Map<string, string>();
  return {
    getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, String(v)),
    removeItem: (k) => void m.delete(k), clear: () => m.clear(),
    key: (i) => [...m.keys()][i] ?? null, get length() { return m.size; },
  };
}

beforeEach(() => {
  resetPolls();
  vi.stubGlobal("localStorage", fakeStorage());
  rows = [trade()];
  risk = {};
  news = [];
  entries = [];
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method ?? "GET";
    calls.push({ url: u, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const json = u.includes("/api/history/today") ? { date: "2026-09-15" }
      : u.includes("/api/settings/risk") ? risk
      : u.includes("/api/calendar/news") ? { events: news }
      : u.includes("/api/calendar/journal") && method === "PUT" ? {}
      : u.includes("/api/calendar/journal") ? { entries }
      : { rows, error: null,
          curve: { points: [], net: 0, peak: 0, max_drawdown: 0, trades: 0 } };
    return { ok: true, status: 200, json: async () => json };
  }));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("the cell figure", () => {
  it("switches between money, pips and R, and remembers the choice", async () => {
    render(<CalendarSection days={30} />);
    expect(await screen.findByTestId("day-metric-2026-09-15")).toHaveTextContent("$50.00");

    await userEvent.click(screen.getByRole("button", { name: "Pips" }));
    expect(screen.getByTestId("day-metric-2026-09-15")).toHaveTextContent("+10.0p");

    await userEvent.click(screen.getByRole("button", { name: "R" }));
    expect(screen.getByTestId("day-metric-2026-09-15")).toHaveTextContent("+1.25R");
    expect(localStorage.getItem("calendar/metric")).toBe("r");
  });

  it("opens on the figure chosen last time", async () => {
    localStorage.setItem("calendar/metric", "pips");
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("day-metric-2026-09-15")).toHaveTextContent("+10.0p");
  });
});

describe("markers on the day", () => {
  it("marks a day that reached the daily goal", async () => {
    risk = { daily_goal_enabled: 1, daily_goal_mode: "usd", daily_goal_value: 40 };
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("goal-mark-2026-09-15")).toBeInTheDocument();
  });

  it("marks no goal when the goal is off", async () => {
    risk = { daily_goal_enabled: 0, daily_goal_mode: "usd", daily_goal_value: 40 };
    render(<CalendarSection days={30} />);
    await screen.findByTestId("day-2026-09-15");
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/settings/risk"))).toBe(true));

    expect(screen.queryByTestId("goal-mark-2026-09-15")).not.toBeInTheDocument();
  });

  it("marks a day with news, and asks for the month shown", async () => {
    news = [{ day: "2026-09-16", ts: 1789560000, title: "CPI", currency: "USD", impact: "high" }];
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("news-mark-2026-09-16")).toBeInTheDocument();
    expect(calls.some((c) => c.url.includes("/api/calendar/news?start=2026-09-01&end=2026-09-31")))
      .toBe(true);
  });

  it("marks a day with a journal note", async () => {
    entries = [{ day: "2026-09-15", note: "Overtraded", tags: [], mood: 2, updated_at: 1 }];
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("note-mark-2026-09-15")).toBeInTheDocument();
  });
});

describe("the day's detail", () => {
  it("shows the day's stats and its realised curve", async () => {
    rows = [trade({ ticket: 1, pnl: 60, close_ts: 100 }), trade({ ticket: 2, pnl: -30, close_ts: 200 })];
    render(<CalendarSection days={30} />);

    await userEvent.click(await screen.findByTestId("day-2026-09-15"));

    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByTestId("stat-pf")).toHaveTextContent("2.00");
    expect(dialog.getByTestId("stat-max-dd")).toHaveTextContent("30.00");
    expect(dialog.getByTestId("intraday-curve")).toBeInTheDocument();
  });

  it("saves the journal with its tags split and its mood", async () => {
    render(<CalendarSection days={30} />);
    await userEvent.click(await screen.findByTestId("day-2026-09-15"));
    const dialog = within(screen.getByRole("dialog"));

    await userEvent.type(dialog.getByLabelText("Journal note"), "Clean day");
    await userEvent.type(dialog.getByLabelText("Tags"), "patient, london ,, patient");
    await userEvent.click(dialog.getByRole("button", { name: "Mood 4 of 5" }));
    await userEvent.click(dialog.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.find((c) => c.method === "PUT")).toBeDefined());
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toContain("/api/calendar/journal/2026-09-15");
    expect(put.body).toEqual({ note: "Clean day", tags: ["patient", "london"], mood: 4 });
  });
});

describe("the summary under the month", () => {
  it("counts green and red days", async () => {
    rows = [trade({ ticket: 1, day: "2026-09-15" }),
            trade({ ticket: 2, day: "2026-09-16", pnl: -10 })];
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("streak-days")).toHaveTextContent("1 / 1");
  });
});

describe("the year view", () => {
  it("draws the year and opens a month from it", async () => {
    render(<CalendarSection days={30} />);
    await screen.findByTestId("calendar-month");

    await userEvent.click(screen.getByRole("button", { name: "Year" }));
    expect(await screen.findByTestId("calendar-year")).toHaveTextContent("2026");
    // Its own 365-day read, whatever window the tab is on.
    expect(calls.some((c) => c.url.includes("/api/history/trades?days=365"))).toBe(true);

    await userEvent.click(screen.getByTestId("year-month-3"));
    expect(screen.getByTestId("calendar-month")).toHaveTextContent("March 2026");
  });
});
