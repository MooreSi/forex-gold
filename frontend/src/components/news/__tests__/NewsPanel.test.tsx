import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NewsPanel } from "../NewsPanel";
import { resetPolls } from "@/hooks/usePoll";
import type { NewsState } from "@/api/types";

const HOUR = 3600;
const NOW = 1_750_000_000;

function event(over: Partial<NewsState["events"][number]> = {}) {
  return {
    title: "US Non-Farm Payrolls", currency: "USD", impact: "high",
    ts: NOW + HOUR, forecast: "180K", previous: "175K", score: 9.5, ...over,
  };
}

function state(over: Partial<NewsState> = {}): NewsState {
  return {
    events: [event()],
    current: null,
    blackout: { enabled: true, impact: "high", minutes_before: 15, minutes_after: 15 },
    pause: {},
    ...over,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;

function serve(body: NewsState) {
  fetchMock.mockImplementation(async () => ({
    ok: true, status: 200, json: async () => body,
  }));
}

beforeEach(() => {
  resetPolls();
  vi.setSystemTime(new Date(NOW * 1000));
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  serve(state());
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the banner", () => {
  it("says a blackout is running, and how long is left", async () => {
    serve(state({
      current: { ...event(), mins_remaining: 8, mins_to_event: -2 },
    }));
    render(<NewsPanel />);

    const banner = await screen.findByTestId("news-banner");
    expect(banner).toHaveAttribute("data-state", "blackout");
    expect(banner).toHaveTextContent("Blackout active");
    expect(banner).toHaveTextContent("resumes in 8m");
  });

  it("shows the next release when no blackout is running", async () => {
    render(<NewsPanel />);

    const banner = await screen.findByTestId("news-banner");
    expect(banner).toHaveAttribute("data-state", "upcoming");
    expect(banner).toHaveTextContent("US Non-Farm Payrolls");
  });

  it("says so plainly when the week has nothing left", async () => {
    serve(state({ events: [] }));
    render(<NewsPanel />);

    const banner = await screen.findByTestId("news-banner");
    expect(banner).toHaveAttribute("data-state", "clear");
  });
});

describe("the event list", () => {
  it("renders times in UTC and says which zone they are in", async () => {
    render(<NewsPanel />);

    // NOW + 1h is 2026-06-15 16:06 UTC. The calendar publishes in UTC and the
    // blackout windows are computed in it, so rendering London time would put
    // an event an hour from where the engine thinks it is for half the year.
    expect(await screen.findByText(/16:06 UTC/)).toBeInTheDocument();
    expect(screen.getByText(/Sun 15 Jun/)).toBeInTheDocument();
  });

  it("filters to gold-relevant currencies by default", async () => {
    serve(state({ events: [event(), event({ title: "NZ Trade Balance", currency: "NZD" })] }));
    render(<NewsPanel />);

    await screen.findByText("US Non-Farm Payrolls");
    expect(screen.queryByText("NZ Trade Balance")).toBeNull();
  });

  it("shows the rest once the gold filter is turned off", async () => {
    serve(state({ events: [event(), event({ title: "NZ Trade Balance", currency: "NZD" })] }));
    render(<NewsPanel />);
    await screen.findByText("US Non-Farm Payrolls");

    await userEvent.click(screen.getByLabelText("Gold-relevant currencies only"));

    expect(await screen.findByText("NZ Trade Balance")).toBeInTheDocument();
  });

  it("hides an event that has already happened", async () => {
    serve(state({ events: [event({ title: "Yesterday's CPI", ts: NOW - 5 * HOUR })] }));
    render(<NewsPanel />);

    await screen.findByTestId("news-banner");
    expect(screen.queryByText("Yesterday's CPI")).toBeNull();
  });

  it("shows it again once upcoming-only is turned off", async () => {
    serve(state({ events: [event({ title: "Yesterday's CPI", ts: NOW - 5 * HOUR })] }));
    render(<NewsPanel />);
    await screen.findByTestId("news-banner");

    await userEvent.click(screen.getByLabelText("Upcoming only"));

    expect(await screen.findByText("Yesterday's CPI")).toBeInTheDocument();
  });
});

describe("the blackout window", () => {
  it("shows the settings the calendar reports", async () => {
    serve(state({
      blackout: { enabled: false, impact: "high", minutes_before: 30, minutes_after: 45 },
    }));
    render(<NewsPanel />);

    expect(await screen.findByLabelText("Minutes before")).toHaveValue("30");
    expect(screen.getByLabelText("Minutes after")).toHaveValue("45");
    expect(
      screen.getByLabelText("Hold automated entries around high-impact news"),
    ).not.toBeChecked();
  });

  it("saves the three keys the calendar reads", async () => {
    render(<NewsPanel />);
    const before = await screen.findByLabelText("Minutes before");

    await userEvent.clear(before);
    await userEvent.type(before, "25");
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    await waitFor(() => {
      const put = fetchMock.mock.calls.find((c) => c[1]?.method === "PUT");
      expect(put).toBeTruthy();
      expect(put![0]).toBe("/api/news/blackout");
      expect(JSON.parse(put![1].body)).toEqual({
        enabled: true, minutes_before: 25, minutes_after: 15,
      });
    });
  });

  it("does not save until the button is pressed", async () => {
    render(<NewsPanel />);
    const before = await screen.findByLabelText("Minutes before");

    await userEvent.clear(before);
    await userEvent.type(before, "25");

    expect(fetchMock.mock.calls.filter((c) => c[1]?.method === "PUT")).toHaveLength(0);
  });

  it("says manual orders are never held", async () => {
    render(<NewsPanel />);

    expect(
      await screen.findByText(/Manual orders are never held/),
    ).toBeInTheDocument();
  });
});

describe("refreshing", () => {
  it("asks the server to drop its cache", async () => {
    render(<NewsPanel />);
    await screen.findByTestId("news-banner");

    await userEvent.click(screen.getByTitle("Re-fetch the calendar"));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(
        (c) => c[0] === "/api/news/refresh" && c[1]?.method === "POST",
      )).toBe(true);
    });
  });

  it("does not drop the cache on an ordinary poll", async () => {
    render(<NewsPanel />);
    await screen.findByTestId("news-banner");

    expect(fetchMock.mock.calls.every((c) => c[0] !== "/api/news/refresh")).toBe(true);
  });
});
