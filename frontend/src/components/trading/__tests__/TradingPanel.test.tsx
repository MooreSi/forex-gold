/**
 * The Trading tab's non-order surfaces: the schedule and the EA templates.
 *
 * The order dialogs have their own files. What is tested here is the wiring
 * that decides whether an order is ALLOWED to happen later — the schedule
 * windows, the whole-day target and its today-only override — and the template
 * writes that change how a future trade is managed.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TradingPanel } from "../TradingPanel";
import { resetPolls } from "@/hooks/usePoll";

const SCHEDULE = {
  schedule: {
    mon: [
      { start: "08:00", end: "12:00", enabled: true },
      { start: "13:00", end: "17:00", enabled: false },
    ],
  },
  enabled: true,
  daily_target: 250,
  daily_state: { reached: false, overridden: false, pnl: 40, target: 250 },
  clock: { label: "Broker time (UTC+3)", offset_minutes: 180 },
};

const TEMPLATES = {
  templates: [{ name: "Grid Runner", anchor_pips: 12 }],
  builtin: "Shipped Default",
  ea_connected: true,
  ea_last_seen_secs: 3.2,
};

let fetchMock: ReturnType<typeof vi.fn>;
let schedule: typeof SCHEDULE;
let templates: {
  templates: Record<string, unknown>[];
  builtin: string;
  ea_connected: boolean;
  ea_last_seen_secs: number | null;
};

beforeEach(() => {
  resetPolls();
  schedule = structuredClone(SCHEDULE);
  templates = structuredClone(TEMPLATES);
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") {
      return { ok: true, status: 200, json: async () => ({ pushed: templates.ea_connected }) };
    }
    if (url.startsWith("/api/schedule/state")) {
      return { ok: true, status: 200, json: async () => schedule };
    }
    if (url.startsWith("/api/trading/templates")) {
      return { ok: true, status: 200, json: async () => templates };
    }
    if (url.startsWith("/api/trading/halt")) {
      return {
        ok: true, status: 200,
        json: async () => ({ reason: "", market_closed: false, circuit_breaker: {} }),
      };
    }
    return { ok: true, status: 200, json: async () => [] };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const writes = () => fetchMock.mock.calls.filter((c) => c[1]?.method && c[1].method !== "GET");

describe("the schedule", () => {
  it("says which clock the windows are measured in", async () => {
    // simon-handover/017 asked exactly this. A schedule screen that does not
    // answer it is describing hours in an unknown timezone.
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Schedule" }));

    expect(await screen.findByText(/Broker time \(UTC\+3\)/)).toBeInTheDocument();
  });

  it("shows each day's windows with their own enabled state", async () => {
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Schedule" }));

    expect(await screen.findByLabelText("Mon window 1")).toBeChecked();
    expect(screen.getByLabelText("Mon window 2")).not.toBeChecked();
    expect(screen.getByLabelText("Mon window 1 start")).toHaveValue("08:00");
  });

  it("sends the whole grid when one window changes, not just the window", async () => {
    // The backend stores the schedule as one object. A partial write would
    // drop every day the operator did not touch.
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Schedule" }));

    await userEvent.click(await screen.findByLabelText("Mon window 2"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    const body = JSON.parse(writes()[0][1].body);
    expect(body.schedule.mon).toHaveLength(2);
    expect(body.schedule.mon[1].enabled).toBe(true);
    expect(body.schedule.mon[0].start).toBe("08:00");
  });

  it("turns the whole schedule off by sending false", async () => {
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Schedule" }));

    await userEvent.click(await screen.findByLabelText("Only trade inside these windows"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ enabled: false });
  });

  it("says nothing about the daily target while it has not been reached", async () => {
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Schedule" }));

    await screen.findByLabelText("Whole-day profit target");
    expect(screen.queryByTestId("daily-target-reached")).toBeNull();
  });

  it("says entries are held once the day's target is reached", async () => {
    schedule.daily_state = { reached: true, overridden: false, pnl: 300, target: 250 };
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Schedule" }));

    const badge = await screen.findByTestId("daily-target-reached");
    expect(badge).toHaveTextContent("$300.00");
    expect(badge).toHaveTextContent("automated entries are held");
  });

  it("offers to resume, and says the override is for today only", async () => {
    schedule.daily_state = { reached: true, overridden: true, pnl: 300, target: 250 };
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Schedule" }));

    await userEvent.click(await screen.findByRole("button", { name: /Resume for today/ }));

    await waitFor(() => {
      expect(writes().some((c) => c[0] === "/api/schedule/resume-today")).toBe(true);
    });
    expect(screen.getByText(/clears at the day boundary/)).toBeInTheDocument();
  });

  it("says a target of zero turns the gate off", async () => {
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Schedule" }));

    expect(await screen.findByText("0 turns this gate off")).toBeInTheDocument();
  });
});

describe("EA templates", () => {
  it("says an EA is connected and when it was last heard", async () => {
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "EA templates" }));

    const status = await screen.findByTestId("ea-status");
    expect(status).toHaveAttribute("data-connected", "true");
    expect(status).toHaveTextContent("last heard 3s ago");
  });

  it("says templates still save when no EA is connected", async () => {
    // Saved-but-not-pushed is a normal outcome, not an error.
    templates.ea_connected = false;
    templates.ea_last_seen_secs = null;
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "EA templates" }));

    expect(await screen.findByText(/apply on the next signal/)).toBeInTheDocument();
  });

  it("saves an edited template and reports that it was pushed", async () => {
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "EA templates" }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));

    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(writes().some((c) => String(c[0]).includes("Grid%20Runner"))).toBe(true);
    });
    expect(await screen.findByRole("status")).toHaveTextContent("pushed to the connected EA");
  });

  it("reports a save that could not be pushed as a save, not a failure", async () => {
    templates.ea_connected = false;
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "EA templates" }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));

    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Saved. No EA is connected",
    );
  });

  it("refuses to save unparseable values rather than sending them", async () => {
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "EA templates" }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));

    const box = screen.getByLabelText("Grid Runner values");
    await userEvent.clear(box);
    await userEvent.type(box, "not json");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("status")).toHaveTextContent("nothing was saved");
    expect(writes()).toHaveLength(0);
  });

  it("deletes by name", async () => {
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "EA templates" }));

    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() => {
      const del = writes().find((c) => c[1]?.method === "DELETE");
      expect(del).toBeTruthy();
      expect(String(del![0])).toContain("Grid%20Runner");
    });
  });
});


describe("editing a pending signal", () => {
  const SIGNALS = [
    { signal_id: "S-1", source: "GoldSignals", direction: "BUY", entry_low: 2430, entry_high: 2432, stop_loss: 2421, status: "pending" },
    { signal_id: "S-2", source: "GoldSignals", direction: "SELL", entry_low: 2450, entry_high: 2452, stop_loss: 2461, status: "pending" },
  ];

  beforeEach(() => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method && init.method !== "GET") {
        return { ok: true, status: 200, json: async () => ({}) };
      }
      if (url.startsWith("/api/trading/signals")) {
        return { ok: true, status: 200, json: async () => SIGNALS };
      }
      if (url.startsWith("/api/schedule/state")) {
        return { ok: true, status: 200, json: async () => schedule };
      }
      if (url.startsWith("/api/trading/templates")) {
        return { ok: true, status: 200, json: async () => templates };
      }
      if (url.startsWith("/api/trading/halt")) {
        return {
          ok: true, status: 200,
          json: async () => ({ reason: "", market_closed: false, circuit_breaker: {} }),
        };
      }
      return { ok: true, status: 200, json: async () => [] };
    });
  });

  it("opens the row it was asked to open", async () => {
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Signals" }));

    const rows = await screen.findAllByRole("button", { name: "Edit" });
    await userEvent.click(rows[1]);

    expect(await screen.findByText("Edit signal S-2")).toBeInTheDocument();
    expect(screen.getByLabelText("Entry low")).toHaveValue("2450");
  });

  it("writes to the row it opened, with the id in the URL", async () => {
    // The NiceGUI editor read fifteen widgets out of an enclosing loop, so
    // without explicit captures Save on one row wrote another row's values.
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Signals" }));
    const rows = await screen.findAllByRole("button", { name: "Edit" });
    await userEvent.click(rows[1]);

    await userEvent.click(screen.getByRole("button", { name: /Save this signal/ }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/trading/signals/S-2");
    expect(JSON.parse(writes()[0][1].body)).toMatchObject({ entry_low: 2450 });
  });

  it("does not carry the previous row's draft into the next one", async () => {
    // The React version of the same bug: a dialog that kept its state would
    // show — and save — the first row's numbers under the second row's id.
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Signals" }));
    const rows = await screen.findAllByRole("button", { name: "Edit" });

    await userEvent.click(rows[0]);
    const low = await screen.findByLabelText("Entry low");
    await userEvent.clear(low);
    await userEvent.type(low, "9999");
    await userEvent.click(screen.getByRole("button", { name: /Cancel/ }));

    await userEvent.click((await screen.findAllByRole("button", { name: "Edit" }))[1]);

    expect(await screen.findByLabelText("Entry low")).toHaveValue("2450");
  });

  it("sends a cleared field as null rather than as zero", async () => {
    // Zero is a price. "Not set" is not.
    render(<TradingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Signals" }));
    await userEvent.click((await screen.findAllByRole("button", { name: "Edit" }))[0]);

    await userEvent.clear(screen.getByLabelText("TP1"));
    await userEvent.click(screen.getByRole("button", { name: /Save this signal/ }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body).tp1).toBeNull();
  });
});
