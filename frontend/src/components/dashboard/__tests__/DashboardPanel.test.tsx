/**
 * The Dashboard tab: one screen that answers "what is happening right now"
 * without moving between tabs.
 *
 * What is tested here is what the screen SAYS and what it COSTS, because both
 * are where a summary screen goes wrong:
 *
 * - a summary that invents a number is worse than one that shows nothing, so
 *   every figure it cannot read is an em dash and never a zero;
 * - a dashboard is the tab left open all day, so it must not bill a model and
 *   must not open a second poll against an endpoint the shell already reads.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardPanel } from "../DashboardPanel";
import { resetPolls } from "@/hooks/usePoll";
import { touchedAfterRemove } from "@/test/chartStub";

vi.mock("lightweight-charts", async () => (await import("@/test/chartStub")).chartStub());

const TICK = {
  bid: 2438.3, ask: 2438.7, mid: 2438.5, spread: 0.4, spread_points: 40,
  timestamp: 1_750_000_000, source: "mt5",
};

/** Two daily candles: yesterday, and the one forming now. */
let dailyCandles: Record<string, unknown>[];
let headerBody: Record<string, unknown>;
let tradesBody: Record<string, unknown>[];
let fetchMock: ReturnType<typeof vi.fn>;

function header(over: Record<string, unknown> = {}) {
  return {
    account: { balance: 10250.44, equity: 10310.2, margin_free: 9800 },
    bridge: { connected: true },
    tick: TICK,
    lifetime_pnl: 1482.13,
    active_trader: "local",
    pause: { paused: false, reason: "", until: null, source: "" },
    remote_connected: false,
    ea_badge: null,
    update: null,
    ...over,
  };
}

const HISTORY = {
  days: 30,
  performance: {
    balance: 10250.44, closed_trades: 48, win_rate_pct: 72.9,
    total_net_pnl: 1482.19, profit_factor: 2.18, max_drawdown_pct: 6.2,
  },
  hourly: [], channels: [], ladder: {},
};

const CLOSED = {
  rows: [], error: null,
  curve: {
    points: [
      { ts: 1_749_000_000, pnl: 120 },
      { ts: 1_749_500_000, pnl: 410 },
      { ts: 1_750_000_000, pnl: 1482.19 },
    ],
    net: 1482.19, peak: 1500, max_drawdown: 88, trades: 48,
  },
};

const SETFORGET = {
  generated_at: 1_750_000_000,
  price: 2438.5,
  evidence: {
    price: 2438.5, weekly_bias: "bullish", daily_bias: "bullish",
    entry_bias: "bullish", entry_timeframe: "4H",
    zones: [{ kind: "demand", low: 2420, high: 2425, ts: 1, touches: 3 }],
    atr: 18.4, daily_atr: 31.2, ema_fast: 2430.1, ema_slow: 2401.7,
    rsi: 58.3, fib: 0.5, fib_levels: [], impulse: null, confirmation: null,
  },
  candidate: null, no_setup_reason: "", invalidations: [],
  confluence: { items: [], score: 6, max: 9, pct: 68, grade: "moderate" },
  ai: null, billed: false, risk_per_lot: null, reward_per_lot: null,
  suggested_lot: null, lot_size: 0.1, risk_per_trade_pct: 1, balance: null,
  min_rr: 1.5, preferred_rr: 2, strategy: "setforget", source_name: "SetForget",
  control_target: "local", ai_configured: false, ai_provider: "", ai_model: "",
};

const RISK = {
  risk_per_trade_pct: 1.0, max_daily_loss_pct: 3.0, max_open_trades: 5,
  max_lot_size: 0.5,
};

function jsonFor(url: string): unknown {
  if (url.startsWith("/api/system/header")) return headerBody;
  if (url.startsWith("/api/chart/candles")) {
    return url.includes("timeframe=1D") ? dailyCandles : [];
  }
  if (url.startsWith("/api/chart/overlays")) {
    return { timeframe: "5m", count: 0, emas: {}, rsi: [], fvgs: [] };
  }
  // The chart's own view of the open trades carries NO running profit --
  // `/api/chart/trades` answers `pnl: null` -- so these two payloads differ
  // on purpose. See the positions test.
  if (url.startsWith("/api/chart/trades")) {
    return tradesBody.map((t) => ({ ...t, pnl: null }));
  }
  if (url.startsWith("/api/trading/trades")) return tradesBody;
  if (url.startsWith("/api/chart/tick")) return TICK;
  if (url.startsWith("/api/history/state")) return HISTORY;
  if (url.startsWith("/api/history/trades")) return CLOSED;
  if (url.startsWith("/api/trading/setforget")) return SETFORGET;
  if (url.startsWith("/api/trading/signals")) {
    return [
      { id: "1", source: "ICT Signals", direction: "SELL", entry: 2438.2, status: "open" },
      { id: "2", source: "ForexTelegraph", direction: "BUY", entry: 2412.6, status: "closed" },
    ];
  }
  if (url.startsWith("/api/settings/risk")) return RISK;
  if (url.startsWith("/api/engines/state")) {
    return {
      engines: [{ id: "breakout", label: "Breakout", running: true, built: true }],
      settings: {},
    };
  }
  if (url.startsWith("/api/schedule/state")) {
    return { markets: { session: "london", allowed_now: true, london: true } };
  }
  if (url.startsWith("/api/news/state")) {
    return {
      events: [], current: null,
      blackout: { enabled: true, impact: "high", minutes_before: 15, minutes_after: 15 },
      pause: {},
    };
  }
  if (url.startsWith("/api/ai/research")) return { analysis: null, saved_at: "" };
  return {};
}

beforeEach(() => {
  resetPolls();
  touchedAfterRemove.length = 0;
  headerBody = header();
  tradesBody = [
    { id: 7, direction: "BUY", entry: 2410.5, lots: 0.1, pnl: 28.4, tg_source: "ICT Signals" },
  ];
  dailyCandles = [
    { ts: 1_749_900_000, open: 2400, high: 2440, low: 2395, close: 2426.1 },
    { ts: 1_750_000_000, open: 2426.1, high: 2441, low: 2424, close: 2438.5 },
  ];
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => jsonFor(url) };
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const getsTo = (prefix: string) =>
  fetchMock.mock.calls.filter(
    (c) => String(c[0]).startsWith(prefix) && (!c[1]?.method || c[1].method === "GET"),
  );

describe("the price it leads with", () => {
  it("shows the live mid from the header poll", async () => {
    render(<DashboardPanel />);

    expect(await screen.findByTestId("dash-price")).toHaveTextContent("2438.50");
  });

  it("measures today's move from the daily open, not from zero", async () => {
    // 2438.50 against a 2426.10 open is +12.40, which is +0.51%. Measuring it
    // from anything else -- the previous close, the window's first candle --
    // is a different number that looks equally plausible.
    render(<DashboardPanel />);

    const change = await screen.findByTestId("dash-change");
    expect(change).toHaveTextContent("+12.40");
    expect(change).toHaveTextContent("0.51%");
  });

  it("shows an em dash, not +0.00, when the daily open cannot be read", async () => {
    // A flat day and a day nobody could measure are different facts. "+0.00%"
    // for the second is the most confident possible wrong answer.
    dailyCandles = [];
    render(<DashboardPanel />);

    await screen.findByTestId("dash-price");
    await waitFor(() =>
      expect(screen.getByTestId("dash-change")).toHaveTextContent("—"));
  });

  it("shows an em dash for the price itself when the bridge has no tick", async () => {
    headerBody = header({ tick: null, account: null });
    render(<DashboardPanel />);

    await waitFor(() => expect(screen.getByTestId("dash-price")).toHaveTextContent("—"));
  });
});

describe("what a visit costs", () => {
  it("never asks a model for anything", async () => {
    // The dashboard is the tab left open all day. Every AI figure on it is
    // read back from what the AI Analysis tab already produced; opening this
    // screen must not spend a penny.
    render(<DashboardPanel />);
    await screen.findByTestId("dash-price");

    const billable = fetchMock.mock.calls.filter(
      (c) => c[1]?.method && c[1].method !== "GET");
    expect(billable).toEqual([]);
  });

  it("asks for daily candles at the count the endpoint will accept", async () => {
    // `/api/chart/candles` validates `count` as `ge=10`. The first version
    // asked for the two bars it actually reads, which the running app answered
    // with a 422 -- so today's move showed an em dash while the feed had the
    // number all along. A stubbed fetch cannot catch that; this can.
    render(<DashboardPanel />);
    await screen.findByTestId("dash-price");

    const daily = getsTo("/api/chart/candles").map((c) => String(c[0]))
      .filter((u) => u.includes("timeframe=1D"));
    expect(daily).not.toHaveLength(0);
    for (const url of daily) {
      const count = Number(new URL(url, "http://x").searchParams.get("count"));
      expect(count).toBeGreaterThanOrEqual(10);
    }
  });

  it("reads the account through the shell's own header poll, not a second one",
    async () => {
      // `usePoll` dedups by key. Using a private key here would double every
      // header request for as long as the tab is open.
      render(<DashboardPanel />);
      await screen.findByTestId("dash-price");

      expect(getsTo("/api/system/header")).toHaveLength(1);
    });
});

describe("the figures it summarises", () => {
  it("shows the window's win rate and profit factor from the history payload",
    async () => {
      render(<DashboardPanel />);

      expect(await screen.findByText("72.9%")).toBeInTheDocument();
      expect(screen.getByText("2.18")).toBeInTheDocument();
    });

  it("shows the open positions with the broker's own P&L", async () => {
    // Read from `/api/trading/trades`, NOT `/api/chart/trades`. The chart's
    // payload exists to draw markers and answers `pnl: null` for every
    // position, so a card built on it showed an em dash for a number the
    // broker had all along -- seen on a live demo position on 2026-09-22.
    // The two stubs above differ in exactly that field, so this fails if the
    // card goes back to the chart's copy.
    render(<DashboardPanel />);

    expect(await screen.findByTestId("dash-open-count")).toHaveTextContent("1");
    // Twice: on the row, and as the card's running total. With one position
    // open those two figures must be the same number -- a total that is not
    // the sum of what is listed under it is worse than no total.
    expect(screen.getAllByText("+$28.40")).toHaveLength(2);
  });

  it("names the halt instead of looking idle when trading is paused", async () => {
    headerBody = header({
      pause: { paused: true, reason: "daily loss limit", until: null, source: "governor" },
    });
    render(<DashboardPanel />);

    expect(await screen.findByTestId("dash-halt")).toHaveTextContent("daily loss limit");
  });
});
