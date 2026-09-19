import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HistoryPanel } from "../HistoryPanel";
import { resetPolls } from "@/hooks/usePoll";
import type { HistoryState } from "@/api/types";

function state(over: Partial<HistoryState> = {}): HistoryState {
  return {
    days: 30,
    performance: {
      balance: 10250.44, closed_trades: 37, win_rate_pct: 56.8,
      total_net_pnl: 412.19, profit_factor: 1.84, max_drawdown_pct: 8.4,
    },
    hourly: [
      { weekday: 0, hour: 13, session: "NY", pnl: 42.5, n: 3, avg: 14.17 },
      { weekday: 4, hour: 9, session: "London", pnl: -12, n: 2, avg: -6 },
    ],
    channels: [
      { source: "GoldSignals", trades: 20, wins: 12, win_rate: 60, net_pnl: 310, paused: false },
      { source: "NoisyChannel", trades: 9, wins: 2, win_rate: 22.2, net_pnl: -88, paused: true },
    ],
    ladder: { scale_out: { reach: 2.4, n: 18 } },
    ...over,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;
let body: HistoryState;

beforeEach(() => {
  resetPolls();
  body = state();
  fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => body };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const gets = () => fetchMock.mock.calls.filter((c) => !c[1]?.method || c[1].method === "GET");
const writes = () => fetchMock.mock.calls.filter((c) => c[1]?.method && c[1].method !== "GET");

describe("how often it asks", () => {
  it("renders every panel from ONE request", async () => {
    // bugs/030: three panels each polling the bridge produced 388 round-trips
    // in 25 seconds. One consolidated read is the fix, and this is what keeps
    // it that way.
    render(<HistoryPanel />);
    await screen.findByText("+$412.19");

    expect(gets()).toHaveLength(1);
    expect(gets()[0][0]).toBe("/api/history/state?days=30");
  });

  it("asks again for a different window, not for the same one", async () => {
    render(<HistoryPanel />);
    await screen.findByText("+$412.19");

    await userEvent.click(screen.getByRole("button", { name: "365d" }));

    await waitFor(() => {
      expect(gets().some((c) => c[0] === "/api/history/state?days=365")).toBe(true);
    });
  });
});

describe("the headline numbers", () => {
  it("shows the account's P&L, win rate and drawdown", async () => {
    render(<HistoryPanel />);

    expect(await screen.findByText("+$412.19")).toBeInTheDocument();
    expect(screen.getByText("56.8%")).toBeInTheDocument();
    expect(screen.getByText("8.4%")).toBeInTheDocument();
    expect(screen.getByText("1.84")).toBeInTheDocument();
  });

  it("says there is no broker data rather than showing zeros", async () => {
    // A zeroed row reads as a flat month that actually happened.
    body = state({ performance: {} });
    render(<HistoryPanel />);

    expect(await screen.findByText("No broker data for this window")).toBeInTheDocument();
    expect(screen.queryByText("+$0.00")).toBeNull();
  });

  it("still shows the local panels when the broker cannot answer", async () => {
    body = state({ performance: {} });
    render(<HistoryPanel />);
    await screen.findByText("No broker data for this window");

    expect(screen.getByTestId("cell-0-13")).toBeInTheDocument();
  });
});

describe("the heatmap", () => {
  it("places an hour on its weekday and hour", async () => {
    render(<HistoryPanel />);

    const cell = await screen.findByTestId("cell-0-13");
    expect(cell).toHaveAttribute("title", expect.stringContaining("Mon 13:00 UTC"));
    expect(cell).toHaveAttribute("title", expect.stringContaining("NY"));
    expect(cell).toHaveAttribute("title", expect.stringContaining("+$42.50"));
  });

  it("says how many trades an hour is measured from", async () => {
    render(<HistoryPanel />);

    expect(await screen.findByTestId("cell-4-9")).toHaveAttribute(
      "title", expect.stringContaining("2 trades"),
    );
  });

  it("leaves an hour with no trades blank rather than green", async () => {
    // No data and break-even are different answers.
    render(<HistoryPanel />);
    await screen.findByTestId("cell-0-13");

    expect(screen.queryByTestId("cell-2-4")).toBeNull();
  });

  it("says the grid is in UTC", async () => {
    render(<HistoryPanel />);

    expect(await screen.findByText(/UTC\./)).toBeInTheDocument();
  });
});

describe("the channel scorecard", () => {
  it("lists each channel with its numbers", async () => {
    render(<HistoryPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Channels" }));

    expect(screen.getByText("GoldSignals")).toBeInTheDocument();
    expect(screen.getByText("+$310.00")).toBeInTheDocument();
    expect(screen.getByText("-$88.00")).toBeInTheDocument();
  });

  it("shows a paused channel as not taking signals", async () => {
    render(<HistoryPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Channels" }));

    expect(screen.getByLabelText("Take signals from NoisyChannel")).not.toBeChecked();
    expect(screen.getByLabelText("Take signals from GoldSignals")).toBeChecked();
  });

  it("pauses one by unchecking it", async () => {
    render(<HistoryPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Channels" }));

    await userEvent.click(screen.getByLabelText("Take signals from GoldSignals"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      source: "GoldSignals", paused: true,
    });
  });

  it("un-pauses by sending false, not by omitting the flag", async () => {
    // A toggle that only ever sends true can pause a channel and never
    // restore it.
    render(<HistoryPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Channels" }));

    await userEvent.click(screen.getByLabelText("Take signals from NoisyChannel"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      source: "NoisyChannel", paused: false,
    });
  });
});

describe("recomputing", () => {
  it("is a button, and does not happen on a poll", async () => {
    render(<HistoryPanel />);
    await screen.findByText("+$412.19");
    expect(writes()).toHaveLength(0);

    await userEvent.click(screen.getByTitle(/Rebuild the channel scorecard/));

    await waitFor(() => {
      expect(writes().some((c) => String(c[0]).startsWith("/api/history/recompute"))).toBe(true);
    });
  });
});

describe("ladder reach", () => {
  it("shows the sample size beside the average", async () => {
    // A mean over four trades is not a measurement, and the strategy that
    // looks best here is usually the one with the fewest samples.
    render(<HistoryPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Ladder reach" }));

    expect(screen.getByText("TP 2.4")).toBeInTheDocument();
    expect(screen.getByText("18")).toBeInTheDocument();
  });
});

describe("the trades tab", () => {
  it("costs nothing until it is opened", async () => {
    // A row per trade over ten years is what forced the old WebSocket buffer
    // from 1MB to 10MB. It is not on the default tab, so it is not fetched.
    render(<HistoryPanel />);
    await screen.findByText("+$412.19");

    expect(gets().some((c) => String(c[0]).includes("/api/history/trades"))).toBe(false);
  });

  it("asks for the trades of the window that is selected", async () => {
    render(<HistoryPanel />);
    await screen.findByText("+$412.19");

    await userEvent.click(screen.getByRole("button", { name: "365d" }));
    await userEvent.click(screen.getByRole("tab", { name: "Trades" }));

    await waitFor(() => {
      expect(gets().some((c) => c[0] === "/api/history/trades?days=365")).toBe(true);
    });
  });
});
