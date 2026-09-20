import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TradeAnalysisPanel as AiPanel } from "../TradeAnalysisPanel";

/**
 * One page, three subjects.
 *
 * REWRITTEN 2026-09-19. These tests described the React port's three-way
 * selector — "re-reads the evidence when the subject changes", "drops a stale
 * answer when the subject changes" — a design the owner asked to be replaced
 * with the NiceGUI page's single scrolling page. Every property they were
 * actually protecting is kept below, scoped to a section:
 *
 *   * the numbers are readable without paying for an opinion about them;
 *   * no model is called until a SPECIFIC section is asked — one page must
 *     not mean three bills;
 *   * a refusal reaches the screen in the backend's own words;
 *   * an unconfigured provider disables the button with a reason rather than
 *     answering nothing, which reads as a model with no opinion.
 */
const SUBJECTS = {
  subjects: [
    { id: "channels", label: "Telegram channels" },
    { id: "strategies", label: "Fixed strategies vs DPM" },
    { id: "generator", label: "The internal signal generator" },
  ],
  configured: true,
  provider: "anthropic",
  model: "claude-opus-5",
};

// One shape per subject, as the three gatherers really return them.
const EVIDENCE: Record<string, unknown> = {
  channels: [
    {
      channel_name: "GoldSignals",
      stats: {
        total_signals: 40, closed_trades: 30, win_rate_pct: 61.0,
        total_pnl: 310.5, phantom_tp_count: 2,
        simulated_50pct_pnl_sum: 520.25, max_consecutive_losses: 4,
      },
    },
  ],
  strategies: {
    days: 30, total_closed: 421,
    fixed_stats: { count: 421, wins: 201, win_rate: 47.7, total_pnl: -4735.72,
                   avg_pnl: -11.25, profit_factor: 0.61, avg_hold_min: 23,
                   sl_exits: 337, be_exits: 0 },
    dpm_stats: { count: 0, wins: 0, win_rate: 0, total_pnl: 0, avg_pnl: 0,
                 profit_factor: 0, avg_hold_min: 0, sl_exits: 0, be_exits: 0 },
    strategy_breakdown: [
      { strategy: "orb_fixed", count: 5, wins: 3, win_rate: 60.0,
        total_pnl: -25.06, avg_pnl: -5.01, profit_factor: 0.23,
        avg_hold_min: 12, sl_exits: 2 },
    ],
    dpm_detail: { count: 0 },
  },
  generator: {
    days: 30, total_trades: 421,
    engines: [
      {
        strategy: "breakout", label: "Breakout",
        all: { count: 20, win_rate: 55, total_pnl: 120 },
        early_half: { count: 8, win_rate: 40, total_pnl: -30 },
        late_half: { count: 12, win_rate: 66, total_pnl: 150 },
      },
      {
        strategy: "reversal", label: "Reversal",
        all: { count: 4, win_rate: 25, total_pnl: -80 },
        early_half: { count: 0, win_rate: 0, total_pnl: 0 },
        late_half: { count: 4, win_rate: 25, total_pnl: -80 },
      },
    ],
  },
};

let fetchMock: ReturnType<typeof vi.fn>;
let configured: boolean;
let analyseFails: boolean;

beforeEach(() => {
  configured = true;
  analyseFails = false;
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/ai/subjects") {
      return { ok: true, status: 200, json: async () => ({ ...SUBJECTS, configured }) };
    }
    if (url.startsWith("/api/ai/evidence")) {
      const subject = new URL(url, "http://x").searchParams.get("subject") ?? "";
      return { ok: true, status: 200,
               json: async () => ({ evidence: EVIDENCE[subject] ?? null }) };
    }
    if (url === "/api/ai/analyse" && init?.method === "POST") {
      if (analyseFails) {
        return { ok: false, status: 409, statusText: "", json: async () => ({
          error: { kind: "refusal", message: "The provider rejected the API key.", ref: null },
        }) };
      }
      const subject = JSON.parse(String(init.body)).subject;
      return { ok: true, status: 200, json: async () => ({
        answer: JSON.stringify({ overall_assessment: `A verdict about ${subject}.` }),
      }) };
    }
    return { ok: false, status: 404, statusText: "", json: async () => ({}) };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const called = (prefix: string) =>
  fetchMock.mock.calls.filter((c) => String(c[0]).startsWith(prefix));

const section = (id: string) => screen.getByTestId(`subject-${id}`);

describe("one page, not three", () => {
  it("shows all three subjects at once", async () => {
    render(<AiPanel />);

    expect(await screen.findByTestId("subject-channels")).toBeInTheDocument();
    expect(screen.getByTestId("subject-strategies")).toBeInTheDocument();
    expect(screen.getByTestId("subject-generator")).toBeInTheDocument();
  });

  it("loads every subject's evidence without calling a model", async () => {
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    await waitFor(() => expect(called("/api/ai/evidence")).toHaveLength(3));
    expect(called("/api/ai/analyse")).toHaveLength(0);
  });

  it("re-reads every subject when the window changes", async () => {
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    await userEvent.click(screen.getByRole("button", { name: "90d" }));

    await waitFor(() => {
      const at90 = called("/api/ai/evidence").filter((c) => String(c[0]).includes("days=90"));
      expect(at90).toHaveLength(3);
    });
  });
});

describe("the channel table", () => {
  it("shows the measured numbers", async () => {
    render(<AiPanel />);

    expect(await screen.findByText("GoldSignals")).toBeInTheDocument();
    expect(within(section("channels")).getByText("61.0%")).toBeInTheDocument();
    expect(within(section("channels")).getByText("+$310.50")).toBeInTheDocument();
  });

  it("surfaces phantom TPs, which is the number the tab exists for", async () => {
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    expect(within(section("channels")).getByText("Phantom TPs")).toBeInTheDocument();
    expect(within(section("channels")).getByText("2")).toBeInTheDocument();
  });

  it("shows what a 50%-at-TP1 rule would have produced instead", async () => {
    // The most actionable line in the report: a channel whose simulated
    // figure is far better than its actual one is badly managed, not bad.
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    expect(within(section("channels")).getByText("+$520.25")).toBeInTheDocument();
  });
});

describe("the DPM comparison", () => {
  it("puts DPM and the fixed strategies head to head", async () => {
    render(<AiPanel />);
    await screen.findByTestId("fixed-stats");

    expect(within(section("strategies")).getByText("Fixed strategies")).toBeInTheDocument();
    expect(within(section("strategies")).getByText("DPM-managed")).toBeInTheDocument();
  });

  it("says a side with no trades has none rather than showing zeros", async () => {
    // A row of zeros beside a real drawdown reads as the safer choice.
    render(<AiPanel />);

    expect(await screen.findByTestId("dpm-stats"))
      .toHaveTextContent("no trades in this window");
  });

  it("breaks the fixed strategies out by name", async () => {
    render(<AiPanel />);

    expect(await screen.findByTestId("strategy-breakdown")).toHaveTextContent("orb_fixed");
  });
});

describe("the engine comparison", () => {
  it("shows each engine's early half against its late half", async () => {
    // The question the subject exists for is "is it improving", and a single
    // total cannot answer it.
    render(<AiPanel />);
    const row = await screen.findByTestId("engine-breakout");

    expect(within(row).getByTestId("early-breakout")).toHaveTextContent("40%");
    expect(within(row).getByTestId("late-breakout")).toHaveTextContent("66%");
  });

  it("shows a dash for a half with no trades, not 0%", async () => {
    // An engine that did not trade in the first half has not declined.
    render(<AiPanel />);
    await screen.findByTestId("engine-reversal");

    expect(screen.getByTestId("early-reversal")).toHaveTextContent("—");
  });
});

describe("what costs money", () => {
  it("says so in every section, not once at the top", async () => {
    // An operator scrolling to the third section should not have to remember
    // a warning from the first.
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    for (const id of ["channels", "strategies", "generator"]) {
      expect(within(section(id)).getByText(/numbers above are free/)).toBeInTheDocument();
    }
  });

  it("names the model in the header so it is not a surprise", async () => {
    render(<AiPanel />);

    expect(await screen.findByText("anthropic · claude-opus-5")).toBeInTheDocument();
  });

  it("asks for ONE subject when that section's button is pressed", async () => {
    // One page must not mean three bills.
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    await userEvent.click(
      within(section("generator")).getByRole("button", { name: /Ask the model/ }));

    await waitFor(() => expect(called("/api/ai/analyse")).toHaveLength(1));
    expect(JSON.parse(called("/api/ai/analyse")[0][1].body)).toEqual({
      subject: "generator", days: 30,
    });
  });

  it("puts the answer under the section that was asked", async () => {
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    await userEvent.click(
      within(section("strategies")).getByRole("button", { name: /Ask the model/ }));

    await waitFor(() => expect(
      within(section("strategies")).getByTestId("ai-answer")).toBeInTheDocument());
    expect(within(section("channels")).queryByTestId("ai-answer")).toBeNull();
  });

  it("disables every button with a reason when no provider is configured", async () => {
    configured = false;
    render(<AiPanel />);

    const ask = (await screen.findAllByRole("button", { name: /Ask the model/ }))[0]!;
    expect(ask).toBeDisabled();
    expect(ask).toHaveAttribute("title", expect.stringContaining("Settings → AI"));
  });

  it("shows a refusal from the backend verbatim, in its own section", async () => {
    analyseFails = true;
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    await userEvent.click(
      within(section("channels")).getByRole("button", { name: /Ask the model/ }));

    expect(await within(section("channels")).findByRole("alert"))
      .toHaveTextContent("The provider rejected the API key.");
  });

  it("drops the answers when the window changes", async () => {
    // An answer about 30 days sitting under a 90-day table is the kind of
    // thing somebody acts on.
    render(<AiPanel />);
    await screen.findByText("GoldSignals");
    await userEvent.click(
      within(section("channels")).getByRole("button", { name: /Ask the model/ }));
    await within(section("channels")).findByTestId("ai-answer");

    await userEvent.click(screen.getByRole("button", { name: "90d" }));

    await waitFor(() => expect(screen.queryByTestId("ai-answer")).toBeNull());
  });
});
