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
let decisionSummary: Record<string, unknown>;
let decisionVariants: Record<string, unknown>[];

beforeEach(() => {
  resetPolls();
  state = structuredClone(STATE);
  decisionSummary = {
    total: 12, executed: 5, blocked: 7, resolved: 3, awaiting_outcome: 2,
    observed: 10, reconstructed: 2,
    by_path: [{ path: "ime", executed: 2, blocked: 1 },
              { path: "full", executed: 3, blocked: 6 }],
    top_reasons: [{ reason: "outside trading hours", n: 4 }],
  };
  decisionVariants = [
    { variant: "champion", is_champion: true, n_taken: 3, n_skipped: 1,
      n_abstained: 0, mean_r: 0.42, net: 12.5 },
    { variant: "news_gate", is_champion: false, n_taken: 0, n_skipped: 4,
      n_abstained: 0, mean_r: null, net: 0 },
  ];
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") {
      if (String(url).startsWith("/api/decision-log/backfill")) {
        return {
          ok: true, status: 200,
          json: async () => ({ added: 7, note: "Rebuilt 7 past decision(s)." }),
        };
      }
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
    if (url.startsWith("/api/decision-log/summary")) {
      return { ok: true, status: 200, json: async () => decisionSummary };
    }
    if (url.startsWith("/api/decision-log/report")) {
      return { ok: true, status: 200, json: async () => ({ variants: decisionVariants }) };
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
    // 12 until the 2026-09-18 upstream merge added `tg_decision_log_enabled`
    // under a new RESEARCH badge. The number is exact on purpose — the failure
    // above was switches SILENTLY disappearing, so a `>=` here would not have
    // caught it. Change it only alongside a switch that genuinely arrived or
    // went, and say which in the commit.
    expect(PARSING_KEYS).toContain("tg_decision_log_enabled");
    expect(PARSING_KEYS.length).toBe(13);
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


describe("the decision log", () => {
  const open = async () => {
    render(<ParsingPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Decision log" }));
  };

  it("says up front that it changes nothing", async () => {
    // The one thing an operator needs to know before switching it on.
    await open();

    expect(await screen.findByText(/nothing here\s+changes a trade/i)).toBeInTheDocument();
  });

  it("shows what happened without needing a single closed trade", async () => {
    await open();

    expect(await screen.findByText(/12 decisions/)).toBeInTheDocument();
    expect(screen.getByText(/5 executed, 7 declined/)).toBeInTheDocument();
    expect(screen.getByText(/Immediate Market: 2 executed, 1 declined/)).toBeInTheDocument();
    expect(screen.getByText(/outside trading hours/)).toBeInTheDocument();
  });

  it("tells a fresh install where to switch it on", async () => {
    decisionSummary = {
      total: 0, executed: 0, blocked: 0, resolved: 0, awaiting_outcome: 0,
      observed: 0, reconstructed: 0, by_path: [], top_reasons: [],
    };
    await open();

    expect(await screen.findByText(/Nothing recorded yet/)).toBeInTheDocument();
  });

  it("does not fetch the variant report until it is asked for", async () => {
    // It says nothing for days and sits behind a live trading page. Loading
    // it on render is a query per tab switch for a number that moves twice a
    // day.
    await open();
    await screen.findByText(/12 decisions/);

    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/report"))).toBe(false);
  });

  it("shows champion vs challenger when it is", async () => {
    await open();

    await userEvent.click(
      await screen.findByRole("button", { name: "Champion vs challenger" }));

    expect(await screen.findByText(/champion \(live\)/)).toBeInTheDocument();
    expect(screen.getByText(/\+0\.420R/)).toBeInTheDocument();
  });

  it("says 'no decisions yet' rather than 0.000 for a variant with no evidence", async () => {
    // Zero expectancy and no evidence are different statements. Rendering both
    // as 0.000 invites somebody to act on evidence that does not exist.
    await open();

    await userEvent.click(
      await screen.findByRole("button", { name: "Champion vs challenger" }));

    const row = await screen.findByText(/news_gate/);
    expect(row).toHaveTextContent("no decisions yet");
    expect(row).not.toHaveTextContent("0.000R");
  });

  it("rebuilds from past trades and says how many were new", async () => {
    await open();

    await userEvent.click(
      await screen.findByRole("button", { name: "Rebuild from past trades" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Rebuilt 7 past decision(s)");
    expect(writes().some((c) => String(c[0]).includes("/api/decision-log/backfill"))).toBe(true);
  });

  it("warns what the rebuild can and cannot reconstruct", async () => {
    await open();

    const button = await screen.findByRole("button", { name: "Rebuild from past trades" });
    expect(button).toHaveAttribute("title", expect.stringContaining("record no opinion"));
    expect(button).toHaveAttribute("title", expect.stringContaining("Safe to press twice"));
  });
});
