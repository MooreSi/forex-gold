/**
 * Trading > Set & Forget.
 *
 * The section has a button that opens a real position, so the questions here
 * are the ones the ORB card's tests ask, plus two this section adds:
 *
 * 1. Can Execute be triggered by accident, and does it name the numbers first?
 * 2. Are the numbers it sends the ones that were on screen?
 * 3. **Does it refuse to place a setup that breaks the method's own rules?**
 *    A 1:0.6 trade renders exactly as tidily as a 1:3 one — same card, same
 *    confidence — so the refusal is the only thing distinguishing them.
 * 4. **Does a limit setup go to the limit endpoint?** A resting order sent to
 *    `/orders/market` fills immediately at a price the operator never agreed
 *    to. That is the failure mode of a section built around resting orders.
 *
 * Nothing here reaches a broker: `fetch` is a recorder.
 */
import { render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SetForgetPanel } from "../SetForgetPanel";
import { useSetForgetController } from "../hooks/useSetForgetController";
import { resetPolls } from "@/hooks/usePoll";

// lightweight-charts wants a real canvas, a matchMedia and a ResizeObserver;
// jsdom has none of the three. The same stand-in the Chart tab's test uses,
// plus the two calls this section's overlay makes -- `priceToCoordinate` for
// the position box and `priceScale().width()` for its right edge. The box's
// own geometry is asserted in PositionOverlay.test.tsx, against real numbers.
vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CrosshairMode: { Normal: 0 },
  createChart: () => ({
    addCandlestickSeries: () => ({
      setData: () => {},
      applyOptions: () => {},
      priceToCoordinate: (price: number) => 400 - (price - 1900) * 2,
    }),
    addLineSeries: () => ({ setData: () => {} }),
    priceScale: () => ({ width: () => 60 }),
    timeScale: () => ({
      // The real chart has this; the panel opens on the recent bars with it.
      setVisibleLogicalRange: () => {},
      subscribeVisibleTimeRangeChange: () => {},
      unsubscribeVisibleTimeRangeChange: () => {},
    }),
    remove: () => {},
  }),
}));

const CANDIDATE = {
  direction: "BUY", entry: 1985, stop_loss: 1972, take_profit: 2040,
  order_type: "limit", risk: 13, reward: 55, rr: 4.23,
  stage: "triggered",
  trigger: { kind: "shift_of_structure", ts: 1, level: 1972 },
  distance: 15, distance_days: 0.75,
  zone: { kind: "demand", low: 1975, high: 1985, ts: 1, touches: 2 },
  target_zone: { kind: "supply", low: 2040, high: 2050, ts: 2, touches: 1 },
};

const EVIDENCE = {
  price: 2000, weekly_bias: "bullish", daily_bias: "bullish",
  entry_bias: "bullish", entry_timeframe: "4H", zones: [CANDIDATE.zone],
  atr: 6, daily_atr: 20, ema_fast: 1995, ema_slow: 1960, rsi: 52, fib: 0.5,
  fib_levels: [{ ratio: 0.382, price: 1990 }, { ratio: 0.786, price: 1978 }],
  impulse: null, confirmation: null,
};

const CONFLUENCE = {
  items: [
    { id: "htf_agreement", label: "Weekly and Daily agree", weight: 2,
      passed: true, detail: "Both bullish, with the trade." },
    { id: "at_aoi", label: "Price is at an area of interest", weight: 2,
      passed: false, detail: "Price is not at an area of interest." },
  ],
  score: 2, max: 4, pct: 50, grade: "moderate",
};

let state: Record<string, unknown>;
let orderResponse: { status: number; body: unknown };
let fetchMock: ReturnType<typeof vi.fn>;

function baseState(over: Record<string, unknown> = {}) {
  return {
    generated_at: 1_750_000_000, price: 2000, evidence: EVIDENCE,
    candidate: { ...CANDIDATE }, no_setup_reason: "", confluence: CONFLUENCE,
    invalidations: [], ai: null, billed: false,
    risk_per_lot: 1300, reward_per_lot: 5500, suggested_lot: 0.04,
    lot_size: 0.1, risk_per_trade_pct: 1, balance: 5000,
    min_rr: 2, preferred_rr: 3, strategy: "set_and_forget",
    source_name: "Set & Forget", control_target: "local",
    ai_configured: true, ai_provider: "claude", ai_model: "a-model",
    ...over,
  };
}

beforeEach(() => {
  resetPolls();
  state = baseState();
  orderResponse = { status: 200, body: { mt5_ticket: 42, price: 1985 } };
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.startsWith("/api/chart/candles")) {
      return { ok: true, status: 200, json: async () => [] };
    }
    if (url.startsWith("/api/chart/overlays")) {
      return { ok: true, status: 200,
               json: async () => ({ timeframe: "4H", count: 0, emas: {},
                                    rsi: [], fvgs: [] }) };
    }
    if (init?.method && init.method !== "GET") {
      if (url.includes("/setforget/evaluate")) {
        // With no candidate the real endpoint skips the model and bills
        // nothing — see `analysis.evaluate`. A fake that answered with a
        // verdict anyway would hide the case this page reads worst.
        return { ok: true, status: 200,
                 json: async () => (state.candidate
                   ? { ...state, billed: true,
                       ai: { verdict: "take",
                             reasoning: "Daily demand holds.",
                             levels_rejected: [],
                             model: "a-model", error: null } }
                   : { ...state, billed: false, ai: null }) };
      }
      return { ok: orderResponse.status < 400, status: orderResponse.status,
               json: async () => orderResponse.body };
    }
    return { ok: true, status: 200, json: async () => state };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { resetPolls(); vi.unstubAllGlobals(); });

const posts = (fragment: string) =>
  fetchMock.mock.calls.filter(
    ([url, init]) => String(url).includes(fragment)
      && (init as RequestInit | undefined)?.method === "POST");

const sent = (fragment: string) =>
  JSON.parse(String((posts(fragment)[0]?.[1] as RequestInit).body));

async function open() {
  render(<SetForgetPanel />);
  await screen.findByText(/BUY XAUUSD/);
}

describe("placing the trade", () => {
  it("asks before it places anything, naming every number", async () => {
    await open();

    await userEvent.click(screen.getByRole("button", { name: /Place limit order/i }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Rest a BUY limit at 1985\.00\?/))
      .toBeInTheDocument();
    expect(within(dialog).getByText(/1972\.00/)).toBeInTheDocument();
    expect(within(dialog).getByText(/2040\.00/)).toBeInTheDocument();
    // The question alone must not have placed it.
    expect(posts("/api/trading/orders")).toHaveLength(0);
  });

  it("cancelling places nothing", async () => {
    await open();
    await userEvent.click(screen.getByRole("button", { name: /Place limit order/i }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(posts("/api/trading/orders")).toHaveLength(0);
  });

  it("sends a resting setup to the LIMIT endpoint, not the market one", async () => {
    await open();
    await userEvent.click(screen.getByRole("button", { name: /Place limit order/i }));
    await userEvent.click(screen.getByRole("button", { name: /^Place BUY limit$/ }));

    await waitFor(() => expect(posts("/orders/limit")).toHaveLength(1));
    expect(posts("/orders/market")).toHaveLength(0);
    expect(sent("/orders/limit")).toMatchObject({
      direction: "BUY", entry_low: 1985, entry_high: 1985,
      stop_loss: 1972, tp1: 2040, lot_size: 0.1,
    });
  });

  it("sends a market setup to the market endpoint with its strategy tag",
     async () => {
    state = baseState({
      candidate: { ...CANDIDATE, order_type: "market", entry: 1980 },
    });
    await open();
    await userEvent.click(screen.getByRole("button", { name: /Execute/i }));
    await userEvent.click(screen.getByRole("button", { name: /^Open BUY$/ }));

    await waitFor(() => expect(posts("/orders/market")).toHaveLength(1));
    expect(posts("/orders/limit")).toHaveLength(0);
    expect(sent("/orders/market")).toMatchObject({
      direction: "BUY", stop_loss: 1972, take_profit: 2040,
      strategy: "set_and_forget", source_name: "Set & Forget",
    });
  });

  it("sends the levels that were on screen, not a fresh reading", async () => {
    await open();
    await userEvent.click(screen.getByRole("button", { name: /Place limit order/i }));
    // The chart moves while the dialog is open. What is placed must be what
    // was agreed to, not whatever the next poll would have said.
    state = baseState({ candidate: { ...CANDIDATE, stop_loss: 1900,
                                     take_profit: 2500 } });
    await userEvent.click(screen.getByRole("button", { name: /^Place BUY limit$/ }));

    await waitFor(() => expect(posts("/orders/limit")).toHaveLength(1));
    expect(sent("/orders/limit").stop_loss).toBe(1972);
  });

  it("surfaces a refusal from the broker verbatim", async () => {
    orderResponse = {
      status: 409,
      body: { error: { kind: "refusal", message: "Spread too wide: 84 points" } },
    };
    await open();
    await userEvent.click(screen.getByRole("button", { name: /Place limit order/i }));
    await userEvent.click(screen.getByRole("button", { name: /^Place BUY limit$/ }));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("Spread too wide: 84 points");
  });
});

describe("refusing to place what the rules reject", () => {
  it("disables Execute when the setup breaks the method's rules, and says why",
     async () => {
    state = baseState({
      invalidations: ["Reward-to-risk is 1:0.60. Set & Forget does not take "
                      + "anything under 1:2."],
    });
    await open();

    const button = screen.getByRole("button", { name: /Place limit order/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("rules"));
    expect(screen.getByRole("alert")).toHaveTextContent("1:0.60");
  });

  it("disables Execute when there is no setup at all", async () => {
    state = baseState({
      candidate: null,
      no_setup_reason: "The Weekly (bullish) and the Daily (bearish) disagree.",
    });
    render(<SetForgetPanel />);

    expect(await screen.findByText(/No setup right now/)).toBeInTheDocument();
    expect(screen.getByText(
      "The Weekly (bullish) and the Daily (bearish) disagree."))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Execute/i })).toBeDisabled();
  });
});

describe("the evaluation", () => {
  it("bills nothing until the button is pressed", async () => {
    await open();

    expect(posts("/setforget/evaluate")).toHaveLength(0);
    expect(screen.queryByText(/AI review/)).not.toBeInTheDocument();
  });

  it("shows the model's verdict and reasoning once it has run", async () => {
    await open();

    await userEvent.click(screen.getByRole("button", { name: /Evaluate the market/i }));

    expect(await screen.findByText(/AI review: Take it/)).toBeInTheDocument();
    expect(screen.getByText(/Daily demand holds\./)).toBeInTheDocument();
    expect(posts("/setforget/evaluate")).toHaveLength(1);
  });

  /**
   * Reported 2026-09-22: "when i click 'evaluate the market' it just flickers
   * and doesn't appear to analyse the market for a setup".
   *
   * It did analyse. With no candidate the backend deliberately skips the model
   * -- paying to be told what the rules already said is money for nothing --
   * and returns a payload that renders identically to the one already on
   * screen. A button whose entire effect is to repaint the same pixels is
   * indistinguishable from a broken one, so the run has to say it happened.
   */
  it("says the evaluation ran when there was nothing for the model to judge",
    async () => {
      state = baseState({
        candidate: null,
        no_setup_reason: "There is no supply zone above price to rest an "
          + "order at.",
      });
      render(<SetForgetPanel />);
      await screen.findByRole("button", { name: /Evaluate the market/i });

      await userEvent.click(
        screen.getByRole("button", { name: /Evaluate the market/i }));

      const said = await screen.findByRole("status");
      expect(said).toHaveTextContent(/no supply zone above price/i);
      expect(said).toHaveTextContent(/nothing was billed/i);
    });

  it("names the model that was billed when one did run", async () => {
    await open();

    await userEvent.click(
      screen.getByRole("button", { name: /Evaluate the market/i }));

    expect(await screen.findByRole("status")).toHaveTextContent(/a-model/);
  });

  it("says where the button leads when no provider is configured", async () => {
    state = baseState({ ai_configured: false });
    await open();

    const button = screen.getByRole("button", { name: /Evaluate the market/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("Settings > AI"));
  });
});

/**
 * Reported 2026-09-22: "what does refresh do, does it do anything as i cant
 * click it".
 *
 * It did one third of a refresh. It re-read `/api/trading/setforget` and left
 * the chart's own candles on their sixty-second poll, and -- worse -- an
 * earlier evaluation shadows the polled state in the controller, so after
 * pressing Evaluate once the button was genuinely dead. A control that does
 * nothing after the operator has used the page is a control that lies.
 */
describe("refreshing the read", () => {
  it("re-reads the chart's candles too, not only the setup", async () => {
    await open();
    const before = fetchMock.mock.calls.filter(
      ([url]) => String(url).startsWith("/api/chart/candles")).length;

    await userEvent.click(screen.getByRole("button", { name: /Refresh/i }));

    await waitFor(() => expect(fetchMock.mock.calls.filter(
      ([url]) => String(url).startsWith("/api/chart/candles")).length)
      .toBeGreaterThan(before));
  });

  it("drops an evaluation it has replaced rather than showing it forever",
    async () => {
      await open();
      await userEvent.click(
        screen.getByRole("button", { name: /Evaluate the market/i }));
      expect(await screen.findByText(/AI review: Take it/)).toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: /Refresh/i }));

      await waitFor(() =>
        expect(screen.queryByText(/AI review: Take it/)).not.toBeInTheDocument());
    });
});

/**
 * The other half of "it just flickers".
 *
 * Saying what an evaluation did is only worth anything where the person who
 * pressed the button is looking. The three buttons are in the header; the
 * outcome line was rendered last, under the chart, the indicator strip, the
 * no-setup card, the lot selector and the confluence grid -- around two
 * thousand pixels below the fold on the owner's screen. A message nobody
 * scrolls to is the same as no message.
 */
describe("where the outcome is said", () => {
  it("puts the outcome above the chart, not at the foot of the page",
    async () => {
      state = baseState({
        candidate: null,
        no_setup_reason: "There is no supply zone above price.",
      });
      render(<SetForgetPanel />);
      await screen.findByRole("button", { name: /Evaluate the market/i });

      await userEvent.click(
        screen.getByRole("button", { name: /Evaluate the market/i }));

      const said = await screen.findByRole("status");
      const chart = screen.getByTestId("setforget-chart");
      // DOCUMENT_POSITION_FOLLOWING: the chart comes after the message.
      expect(said.compareDocumentPosition(chart)
        & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });
});

describe("the checklist and the sizing", () => {
  it("shows failed items with their reasons rather than hiding them", async () => {
    await open();

    expect(screen.getByText("Price is not at an area of interest."))
      .toBeInTheDocument();
    expect(screen.getByText("Both bullish, with the trade.")).toBeInTheDocument();
  });

  it("re-prices the trade when the lot size changes", async () => {
    await open();

    // 0.1 lots against a $1,300 per-lot risk.
    expect(screen.getByText("$130.00")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "0.50" }));

    await waitFor(() => expect(screen.getByText("$650.00")).toBeInTheDocument());
    // Grouped since 2026-09-19: formatMoney was producing "$1210.40" while
    // the header showed "$4,378.31" two inches away, which is the exact
    // failure format.ts's own docstring warns about.
    expect(screen.getByText("$2,750.00")).toBeInTheDocument();
  });

  it("stores the lot size it was given", async () => {
    await open();

    await userEvent.click(screen.getByRole("button", { name: "0.20" }));

    await waitFor(() => {
      const puts = fetchMock.mock.calls.filter(
        ([, init]) => (init as RequestInit | undefined)?.method === "PUT");
      expect(JSON.parse(String((puts[0][1] as RequestInit).body)))
        .toEqual({ lot_size: 0.2 });
    });
  });
});

/**
 * The controller's callbacks have to hold still.
 *
 * `usePoll` returns a fresh object on every render — it spreads its state and
 * attaches a `refresh`. A `useCallback` that lists that OBJECT as a dependency
 * is therefore rebuilt on every render, and so is the `useMemo` that carries
 * it, and so is every prop this section hands its children. The hook's whole
 * memo goes from "a stable handle" to a new one per keystroke.
 *
 * The polls' own `refresh` functions are stable — they close over the registry
 * entry, which is looked up by key — so the dependency to name is `x.refresh`,
 * not `x`. It was, until the three-way Refresh replaced it on 2026-09-22.
 */
describe("the controller's handles", () => {
  it("hands back the same refresh across a re-render", async () => {
    const { result, rerender } = renderHook(() => useSetForgetController());
    await waitFor(() => expect(result.current.data).not.toBeNull());

    const first = result.current.refresh;
    rerender();

    expect(result.current.refresh).toBe(first);
  });
});
