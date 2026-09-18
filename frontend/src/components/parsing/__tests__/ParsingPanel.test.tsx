import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ParsingPanel } from "../ParsingPanel";
import { PARSING_CATEGORIES, PARSING_KEYS } from "../content/settings";
import { resetPolls } from "@/hooks/usePoll";
import type { ParsingState } from "@/api/types";

const STATE: ParsingState = {
  reader: { auth_state: "CONNECTED" },
  configured: true,
  settings: { immediate_market_entry: 0, lk_enable_tp_parsing: 1 },
  lexicons: { close_all: ["close all", "close everything"] },
  lexicon_labels: { close_all: "Close all triggers" },
  lexicon_help: { close_all: "Phrases that close the channel's open trade." },
  channels: [{ name: "GoldSignals", parser: { enabled: true, learned: ["entry zone"] } }],
};

let fetchMock: ReturnType<typeof vi.fn>;
let state: ParsingState;

beforeEach(() => {
  resetPolls();
  state = structuredClone(STATE);
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    if (url.startsWith("/api/parsing/state")) {
      return { ok: true, status: 200, json: async () => state };
    }
    if (url.startsWith("/api/parsing/messages")) {
      return {
        ok: true, status: 200,
        json: async () => ({
          messages: [{ id: 1, text: "XAUUSD BUY 2430", group_name: "GoldSignals", timestamp: 1_750_000_000 }],
          total: 42,
        }),
      };
    }
    if (url.startsWith("/api/parsing/unrecognised")) {
      return {
        ok: true, status: 200,
        json: async () => ({
          pending: [{ id: 7, raw_text: "zone active", channel_name: "GoldSignals" }],
        }),
      };
    }
    return { ok: false, status: 404, statusText: "", json: async () => ({}) };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const writes = () =>
  fetchMock.mock.calls.filter((c) => c[1]?.method && c[1].method !== "GET");

describe("the settings section", () => {
  it("puts EVERY switch on the screen", async () => {
    // The regression test for the 2026-08-25 merge, which shipped this section
    // as an empty stub: every switch vanished from the UI while staying wired
    // in the backend, so `immediate_market_entry` could not be turned on and a
    // bare "Buy Now" signal was missed. Nothing went red, because the render
    // test pinned the auth wizard and not the settings.
    render(<ParsingPanel />);
    await screen.findByTestId("toggle-auto_execute_signals");

    for (const key of PARSING_KEYS) {
      expect(screen.getByTestId(`toggle-${key}`)).toBeInTheDocument();
    }
    expect(PARSING_KEYS).toContain("immediate_market_entry");
    expect(PARSING_KEYS.length).toBe(12);
  });

  it("puts every category badge on the screen too", async () => {
    render(<ParsingPanel />);
    await screen.findByTestId("toggle-auto_execute_signals");

    for (const category of PARSING_CATEGORIES) {
      expect(screen.getByText(category.badge)).toBeInTheDocument();
    }
  });

  it("gives every switch its description", async () => {
    // A switch with no explanation is one nobody dares touch.
    render(<ParsingPanel />);
    await screen.findByTestId("toggle-immediate_market_entry");

    for (const category of PARSING_CATEGORIES) {
      for (const t of category.toggles) {
        expect(within(screen.getByTestId(`toggle-${t.key}`)).getByText(t.description))
          .toBeInTheDocument();
      }
    }
  });

  it("shows a stored value rather than the default", async () => {
    state.settings = { lk_enable_tp_parsing: 0 };
    render(<ParsingPanel />);

    // Default for this one is ON; the stored row says off.
    expect(await screen.findByLabelText("Enable TP Parsing")).not.toBeChecked();
  });

  it("falls back to the documented default when nothing is stored", async () => {
    state.settings = {};
    render(<ParsingPanel />);

    expect(await screen.findByLabelText("Enable TP Parsing")).toBeChecked();
    expect(screen.getByLabelText("Auto-Execution")).not.toBeChecked();
  });

  it("writes only the switch that changed", async () => {
    render(<ParsingPanel />);

    await userEvent.click(await screen.findByLabelText("Immediate Market Buy/Sell"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/parsing/settings");
    expect(JSON.parse(writes()[0][1].body)).toEqual({ immediate_market_entry: 1 });
  });

  it("saves the match window when the field is left", async () => {
    render(<ParsingPanel />);
    const field = await screen.findByLabelText("Second-message match window");

    await userEvent.clear(field);
    await userEvent.type(field, "600");
    await userEvent.tab();

    await waitFor(() => {
      expect(JSON.parse(writes()[0][1].body)).toEqual({
        lk_second_message_match_window_sec: 600,
      });
    });
  });
});

describe("channels", () => {
  it("lists them with their learned-rule count", async () => {
    render(<ParsingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Channels" }));

    expect(screen.getByText("GoldSignals")).toBeInTheDocument();
    expect(screen.getByText("1 learned rules")).toBeInTheDocument();
  });

  it("toggles one without sending the others", async () => {
    render(<ParsingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Channels" }));

    await userEvent.click(screen.getByLabelText("Parse GoldSignals"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      channel: "GoldSignals", enabled: false,
    });
  });
});

describe("unrecognised messages", () => {
  it("counts them in the header so they are not missed", async () => {
    render(<ParsingPanel />);

    expect(await screen.findByText("1 unread message")).toBeInTheDocument();
  });

  it("teaches the parser when a meaning is chosen", async () => {
    render(<ParsingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Questions" }));

    await userEvent.click(await screen.findByRole("button", { name: "A new signal" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      row_id: 7, status: "resolved", channel: "GoldSignals",
      rule: { pattern: "zone active", means: "entry" },
    });
  });

  it("dismisses without teaching anything when it was not a signal", async () => {
    // Teaching the parser from a message somebody said was NOT a signal is how
    // a channel's chatter starts opening trades.
    render(<ParsingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Questions" }));

    await userEvent.click(await screen.findByRole("button", { name: "Not a signal" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    const body = JSON.parse(writes()[0][1].body);
    expect(body.status).toBe("dismissed");
    expect(body.rule).toBeUndefined();
  });
});

describe("trigger phrases", () => {
  it("saves one category on its own", async () => {
    render(<ParsingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Trigger phrases" }));

    const box = screen.getByLabelText("Close all triggers");
    await userEvent.clear(box);
    await userEvent.type(box, "close all{enter}shut it down");
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      category: "close_all", phrases: ["close all", "shut it down"],
    });
  });
});

describe("the feed", () => {
  it("shows stored messages and how many there are in total", async () => {
    render(<ParsingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Feed" }));

    expect(await screen.findByText("XAUUSD BUY 2430")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });
});

describe("an install with no reader", () => {
  it("says the reader is not configured rather than failing", async () => {
    state.configured = false;
    state.reader = {};
    render(<ParsingPanel />);

    expect(await screen.findByText("reader not configured")).toBeInTheDocument();
  });
});
