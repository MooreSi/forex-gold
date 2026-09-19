import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StrategySection } from "../internal/StrategySection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * Which strategy each signal source trades under.
 *
 * Never ported. Everything it needs was on the backend the whole time, and
 * `GET /api/trading/channel-strategies` answered 500 for months because the
 * handler was annotated `-> dict` and returned a list — nothing called it, so
 * nothing noticed.
 *
 * The two properties worth holding: a paid AI call never happens on its own,
 * and the screen says which mechanism decided a channel's strategy. A row
 * showing a strategy name with no indication of whether it was pinned or
 * chosen automatically is a row you cannot act on.
 */
const CATALOGUE = [
  { key: "scale_out", label: "Scale Out + Breakeven", kind: "builtin", summary: "" },
  { key: "fixed_rr", label: "Fixed R:R", kind: "builtin", summary: "" },
];

let channels: Record<string, unknown>[];
let recommendations: Record<string, unknown>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetPolls();
  channels = [
    { source: "GoldSignals", strategy_override: "scale_out", auto_strategy: false,
      lot_mult: 1.5, win_rate: 60, sample_n: 20, net_pnl: 310 },
    { source: "QuietChannel", strategy_override: null, auto_strategy: true,
      lot_mult: 1, win_rate: 0, sample_n: 0, net_pnl: 0 },
  ];
  recommendations = {};
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST" && url.includes("/recommend")) {
      return { ok: true, status: 200, json: async () => ({
        billable: true,
        recommendations: { QuietChannel: { strategy: "fixed_rr", label: "Fixed R:R" } },
      }) };
    }
    if (init?.method && init.method !== "GET") {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    if (url.startsWith("/api/trading/strategies")) {
      return { ok: true, status: 200, json: async () => ({ catalogue: CATALOGUE, custom: [] }) };
    }
    if (url.includes("/recommendations")) {
      return { ok: true, status: 200, json: async () => ({ recommendations }) };
    }
    return { ok: true, status: 200, json: async () => ({ channels }) };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const writes = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "POST");

describe("what it shows", () => {
  it("lists every channel", async () => {
    render(<StrategySection />);

    expect(await screen.findByTestId("strategy-GoldSignals")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-QuietChannel")).toBeInTheDocument();
  });

  it("says a pinned channel is pinned, not merely which strategy it has", async () => {
    render(<StrategySection />);

    expect(await screen.findByTestId("strategy-GoldSignals")).toHaveTextContent("pinned");
  });

  it("says an automatic channel is automatic", async () => {
    render(<StrategySection />);

    expect(await screen.findByTestId("strategy-QuietChannel")).toHaveTextContent("auto");
  });

  it("shows the channel's record beside the choice", async () => {
    // The scorecard on Analysis is why an operator comes here. Making them
    // switch tabs to remember it is how the wrong channel gets pinned.
    render(<StrategySection />);

    expect(await screen.findByTestId("strategy-GoldSignals")).toHaveTextContent("60% of 20");
  });

  it("says a channel with no closed trades has no record", async () => {
    // Not "0% of 0", which reads as a channel that never wins.
    render(<StrategySection />);

    expect(await screen.findByTestId("strategy-QuietChannel"))
      .toHaveTextContent("no trades yet");
  });
});

describe("assigning one", () => {
  it("sends the channel, the strategy and the auto flag together", async () => {
    render(<StrategySection />);
    const select = await screen.findByLabelText("Strategy for QuietChannel");

    await userEvent.selectOptions(select, "fixed_rr");

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0]![1].body)).toEqual({
      source: "QuietChannel", strategy: "fixed_rr", auto: true,
    });
  });

  it("clearing the override sends null rather than an empty string", async () => {
    // An empty string is a strategy key nothing has. Null is "use the
    // default", which is a different instruction.
    render(<StrategySection />);
    const select = await screen.findByLabelText("Strategy for GoldSignals");

    await userEvent.selectOptions(select, "");

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0]![1].body).strategy).toBeNull();
  });

  it("toggling auto keeps the strategy that is already there", async () => {
    render(<StrategySection />);

    await userEvent.click(await screen.findByLabelText("Auto strategy for GoldSignals"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0]![1].body)).toEqual({
      source: "GoldSignals", strategy: "scale_out", auto: true,
    });
  });
});

describe("the AI recommendation", () => {
  it("never bills without being asked", async () => {
    // The free read happens on load; the paid one must not.
    render(<StrategySection />);
    await screen.findByTestId("strategy-GoldSignals");

    expect(writes().some((c) => String(c[0]).includes("/recommend"))).toBe(false);
  });

  it("says the button costs money before it is pressed", async () => {
    render(<StrategySection />);

    expect(await screen.findByRole("button", { name: /billable/i })).toBeInTheDocument();
  });

  it("shows what was recommended without applying it", async () => {
    recommendations = { GoldSignals: { strategy: "fixed_rr", label: "Fixed R:R" } };
    render(<StrategySection />);

    expect(await screen.findByRole("button", { name: /Fixed R:R/ })).toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });

  it("applies a recommendation only when it is clicked, and pins it", async () => {
    // Applying a suggestion is a decision, so it stops being automatic.
    recommendations = { GoldSignals: { strategy: "fixed_rr", label: "Fixed R:R" } };
    render(<StrategySection />);

    await userEvent.click(await screen.findByRole("button", { name: /Fixed R:R/ }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0]![1].body)).toEqual({
      source: "GoldSignals", strategy: "fixed_rr", auto: false,
    });
  });
});

describe("when there is nothing", () => {
  it("says no channel has sent a signal yet", async () => {
    channels = [];
    render(<StrategySection />);

    expect(await screen.findByText(/No signal sources yet/)).toBeInTheDocument();
  });
});
