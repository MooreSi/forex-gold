import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CalendarSection } from "../internal/CalendarSection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The Analysis Calendar.
 *
 * Its arithmetic is tested in calendarGrid.test.ts. What is tested here is the
 * wiring, and one thing that only shows up wired together: **today comes from
 * the backend**, not from the browser. `/api/history/today` answers on the
 * trading clock, which differs from the machine's date whenever a clock offset
 * is configured — the whole point on a VPS in another timezone. A calendar
 * that highlighted the machine's today would mark the wrong day's trades.
 */
function trade(over: Record<string, unknown> = {}) {
  return {
    ticket: 1, direction: "BUY", entry_price: 2400, exit_price: 2410,
    open_ts: 0, close_ts: Date.UTC(2026, 8, 15, 15, 0) / 1000, lots: 0.1,
    close_lots: [0.1], pnl: 50, fees: 0, pips: null, duration_secs: null,
    order_type: "Market", pending_secs: null, reason: "TP hit",
    source: "GoldSignals", strategy: "", max_tp: "", rr: null,
    spread_points: null, group: null, ...over,
  };
}

let body: Record<string, unknown>;
let todayBody: { date: string };

beforeEach(() => {
  resetPolls();
  body = { rows: [trade()], error: null,
           curve: { points: [], net: 0, peak: 0, max_drawdown: 0, trades: 0 } };
  todayBody = { date: "2026-09-15" };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
    ok: true, status: 200,
    json: async () => (String(url).includes("/today") ? todayBody : body),
  })));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("the month it opens on", () => {
  it("opens on the month of the newest close, not the machine's month", async () => {
    // With a 7-day window in early January, "this month" can be empty while
    // every trade in the window sits in December.
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("calendar-month"))
      .toHaveTextContent("September 2026");
  });

  it("can be stepped back a month", async () => {
    render(<CalendarSection days={30} />);
    await screen.findByTestId("calendar-month");

    await userEvent.click(screen.getByLabelText("Previous month"));

    expect(screen.getByTestId("calendar-month")).toHaveTextContent("August 2026");
  });

  it("steps across a year boundary", async () => {
    body = { ...body, rows: [trade({ close_ts: Date.UTC(2026, 0, 15, 15, 0) / 1000 })] };
    render(<CalendarSection days={30} />);
    await screen.findByTestId("calendar-month");

    await userEvent.click(screen.getByLabelText("Previous month"));

    expect(screen.getByTestId("calendar-month")).toHaveTextContent("December 2025");
  });
});

describe("the days", () => {
  it("puts a trade on its trading day", async () => {
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("day-2026-09-15")).toHaveTextContent("$50.00");
  });

  it("marks a day outside the month as padding", async () => {
    render(<CalendarSection days={30} />);
    await screen.findByTestId("calendar-month");

    expect(screen.getByTestId("day-2026-08-31"))
      .toHaveAttribute("data-in-month", "false");
  });

  it("does not offer to open a day with no trades", async () => {
    render(<CalendarSection days={30} />);
    await screen.findByTestId("calendar-month");

    expect(screen.getByTestId("day-2026-09-16")).toBeDisabled();
  });

  it("totals the month", async () => {
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("calendar-total")).toHaveTextContent("$50.00");
  });
});

describe("today", () => {
  it("marks the day the backend calls today", async () => {
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("day-2026-09-15")).toHaveClass(/ring-accent/);
  });

  it("marks no day when the trading clock cannot be read", async () => {
    // Not the browser's date as a fallback: a wrong "today" is worse than
    // none, because it is silently wrong.
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (String(url).includes("/today")) {
        return { ok: false, status: 500, json: async () => ({}) };
      }
      return { ok: true, status: 200, json: async () => body };
    }));
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("day-2026-09-15")).not.toHaveClass(/ring-accent/);
  });
});

describe("a day's detail", () => {
  it("breaks the day down by signal source", async () => {
    body = { ...body, rows: [
      trade({ ticket: 1, pnl: 50, source: "GoldSignals" }),
      trade({ ticket: 2, pnl: -80, source: "NoisyChannel" }),
    ] };
    render(<CalendarSection days={30} />);

    await userEvent.click(await screen.findByTestId("day-2026-09-15"));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("NoisyChannel")).toBeInTheDocument();
    expect(within(dialog).getByText("GoldSignals")).toBeInTheDocument();
  });

  /**
   * Asked for on 2026-09-22: "when clicking into each day it should also list
   * the profitability per market as well". The NiceGUI page had it — a "By
   * Market Session" table beside the source one — and the React port dropped
   * it. One instrument is traded here, so the market that varies is WHICH ONE
   * WAS OPEN: Asian, London, the overlap, New York.
   *
   * The session is on the row, from the backend. See `trade_table._session`
   * for why it is not derived here.
   */
  it("breaks the day down by market session", async () => {
    body = { ...body, rows: [
      trade({ ticket: 1, pnl: -40, session: "asian" }),
      trade({ ticket: 2, pnl: 90, session: "london" }),
      trade({ ticket: 3, pnl: 10, session: "london" }),
    ] };
    render(<CalendarSection days={30} />);

    await userEvent.click(await screen.findByTestId("day-2026-09-15"));

    const asian = within(screen.getByRole("dialog")).getByTestId("session-asian");
    expect(asian).toHaveTextContent("Asian");
    expect(asian).toHaveTextContent("-$40.00");
    expect(within(screen.getByRole("dialog")).getByTestId("session-london"))
      .toHaveTextContent("$100.00");
  });

  it("says how many trades it could not place in a session", async () => {
    // A row with no close stamp carries no session. Dropping it silently
    // makes the totals disagree with the day's own header for no visible
    // reason; bucketing it makes a quiet session look busy.
    body = { ...body, rows: [
      trade({ ticket: 1, pnl: 50, session: "london" }),
      trade({ ticket: 2, pnl: 10, session: null }),
    ] };
    render(<CalendarSection days={30} />);

    await userEvent.click(await screen.findByTestId("day-2026-09-15"));

    expect(within(screen.getByRole("dialog"))
      .getByText(/1 trade had no close time/i)).toBeInTheDocument();
  });

  it("shows the win rate behind each breakdown row, not just the total",
    async () => {
      // "$10" from one trade in four and "$10" from four in four are not the
      // same day. The NiceGUI table showed wins as "3 / 5" for that reason.
      body = { ...body, rows: [
        trade({ ticket: 1, pnl: 50, source: "GoldSignals" }),
        trade({ ticket: 2, pnl: -40, source: "GoldSignals" }),
      ] };
      render(<CalendarSection days={30} />);

      await userEvent.click(await screen.findByTestId("day-2026-09-15"));

      expect(within(screen.getByRole("dialog")).getByTestId("source-GoldSignals"))
        .toHaveTextContent("1 / 2");
    });

  it("lists the individual trades", async () => {
    render(<CalendarSection days={30} />);

    await userEvent.click(await screen.findByTestId("day-2026-09-15"));

    expect(within(screen.getByRole("dialog")).getByText("TP hit")).toBeInTheDocument();
  });
});

describe("when there is nothing", () => {
  it("says the broker could not answer, separately from a quiet month", async () => {
    body = { rows: [], error: "MT5 deal history is unavailable.",
             curve: { points: [], net: 0, peak: 0, max_drawdown: 0, trades: 0 } };
    render(<CalendarSection days={30} />);

    expect(await screen.findByText(/MT5 deal history is unavailable/)).toBeInTheDocument();
  });

  it("still draws a month when the window is quiet", async () => {
    // A quiet month is information. An empty panel is not.
    body = { rows: [], error: null,
             curve: { points: [], net: 0, peak: 0, max_drawdown: 0, trades: 0 } };
    render(<CalendarSection days={30} />);

    expect(await screen.findByTestId("calendar-month")).toHaveTextContent("September 2026");
  });
});
