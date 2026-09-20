/**
 * The AI Analysis tab: one call, one readable answer.
 *
 * The NiceGUI tab had a single Research Now button. It asked the model ONCE
 * about everything — sentiment, the day's price range, what could move gold,
 * the risks, the levels, which strategy to run — and rendered the answer as
 * cards. The React port put a different page here entirely (the three-subject
 * trade analysis, which in the original lived under the Analysis tab) and
 * printed the model's raw prose. The owner's words on 2026-09-20: "the output
 * was a load of messages, it should be summarised and easy to read".
 *
 * So what these tests protect is: one call, a structured answer, and nothing
 * billed for looking at the tab.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AiPanel } from "../AiPanel";

const ANALYSIS = {
  sentiment: "bullish",
  sentiment_confidence: 0.72,
  today_bias: "Buying dips while 2,640 holds.",
  price_low: 2638.5,
  price_high: 2672.25,
  summary: "Gold is bid into the US session on soft real yields.",
  technical_summary: "Price is above the 21 EMA on H1 with a higher low at 2,641.",
  key_drivers: ["Soft US real yields", "Central bank buying"],
  risk_factors: ["A hot CPI print would reverse this"],
  support_levels: [2640, 2628],
  resistance_levels: [2672, 2690],
  strategy_recommendation: "scale_out",
  strategy_label: "Scale out",
  strategy_reason: "Momentum is good but the range is narrow.",
  signal_analysis: "Four of the last six channel signals aligned with this bias.",
  disclaimer: "AI analysis for informational purposes only. Not financial advice.",
  generated_at: "2026-09-20T09:30:00Z",
};

let stored: Record<string, unknown>;
let fresh: Record<string, unknown>;
let posts: string[];
let failPost: string | null;
/** Held open so a test can look at the screen mid-call. */
let holdPost: Promise<void> | null;

beforeEach(() => {
  posts = [];
  failPost = null;
  holdPost = null;
  stored = { billable: false, analysis: null, saved_at: "" };
  fresh = { billable: true, analysis: ANALYSIS, saved_at: "2026-09-20T09:30:00Z" };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (init?.method === "POST") {
      posts.push(u);
      if (holdPost) await holdPost;
      if (failPost) {
        return {
          ok: false, status: 409,
          json: async () => ({ error: { kind: "refusal", message: failPost } }),
        };
      }
      return { ok: true, status: 200, json: async () => fresh };
    }
    return { ok: true, status: 200, json: async () => stored };
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("opening the tab", () => {
  it("costs nothing", async () => {
    // The button is a decision the operator makes, not a toll for looking.
    render(<AiPanel />);
    await screen.findByRole("button", { name: /research now/i });

    expect(posts).toEqual([]);
  });

  it("shows the analysis this install last ran", async () => {
    stored = { billable: false, analysis: ANALYSIS, saved_at: "2026-09-20T09:30:00Z" };
    render(<AiPanel />);

    expect(await screen.findByText(/soft real yields/i)).toBeInTheDocument();
  });

  it("invites a first run when there has never been one", async () => {
    render(<AiPanel />);

    expect(
      await screen.findByText(/Press Research Now for an AI analysis/i),
    ).toBeInTheDocument();
  });
});

describe("researching", () => {
  it("asks the model once, for everything", async () => {
    // The complaint: the tab broke one question into a call per subject.
    render(<AiPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /research now/i }));

    await waitFor(() => expect(posts).toEqual(["/api/ai/research"]));
  });

  it("says it is working while the call is out", async () => {
    // The call takes as long as the model takes — up to half a minute. A
    // button that looks idle for thirty seconds gets pressed again.
    let release: (() => void) | null = null;
    holdPost = new Promise<void>((r) => { release = r; });
    render(<AiPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /research now/i }));

    await waitFor(() =>
      expect(screen.getByText(/Researching gold market conditions/i)).toBeInTheDocument());
    release!();
  });

  it("puts a refusal on screen in the backend's own words", async () => {
    failPost = "No AI provider is configured. Add a provider and an API key "
      + "under Settings → AI before asking for an analysis.";
    render(<AiPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /research now/i }));

    expect(await screen.findByText(/No AI provider is configured/)).toBeInTheDocument();
  });
});

describe("the answer is summarised, not printed", () => {
  beforeEach(() => {
    stored = { billable: false, analysis: ANALYSIS, saved_at: "2026-09-20T09:30:00Z" };
  });

  it("leads with the sentiment", async () => {
    render(<AiPanel />);

    expect(await screen.findByTestId("sentiment")).toHaveTextContent("BULLISH");
  });

  it("states how sure the model is, as a number", async () => {
    // "Bullish" with no confidence is a claim with no weight behind it.
    render(<AiPanel />);

    expect(await screen.findByTestId("confidence")).toHaveTextContent("72%");
  });

  it("shows the day's price range", async () => {
    render(<AiPanel />);

    const target = await screen.findByTestId("price-target");
    expect(target).toHaveTextContent("2,638.50");
    expect(target).toHaveTextContent("2,672.25");
  });

  it("lists what could move gold", async () => {
    render(<AiPanel />);

    expect(await screen.findByText("Soft US real yields")).toBeInTheDocument();
  });

  it("lists the risks", async () => {
    render(<AiPanel />);

    expect(await screen.findByText(/hot CPI print/)).toBeInTheDocument();
  });

  it("shows the levels it is watching", async () => {
    render(<AiPanel />);

    const levels = await screen.findByTestId("levels");
    expect(levels).toHaveTextContent("2,640.00");
    expect(levels).toHaveTextContent("2,690.00");
  });

  it("names the strategy it recommends, and why", async () => {
    render(<AiPanel />);

    const rec = await screen.findByTestId("strategy-recommendation");
    expect(rec).toHaveTextContent("Scale out");
    expect(rec).toHaveTextContent(/range is narrow/);
  });

  it("carries the disclaimer the model returned", async () => {
    // It is not decoration: this screen is one step from a Place Order button.
    render(<AiPanel />);

    expect(await screen.findByText(/Not financial advice/)).toBeInTheDocument();
  });
});

describe("an answer with holes in it", () => {
  it("renders what there is rather than nothing", async () => {
    // A provider that returns half a schema must not blank the tab.
    stored = {
      billable: false, saved_at: "",
      analysis: { sentiment: "neutral", summary: "Nothing to say today." },
    };
    render(<AiPanel />);

    expect(await screen.findByText("Nothing to say today.")).toBeInTheDocument();
  });

  it("does not invent a price range it was not given", async () => {
    stored = {
      billable: false, saved_at: "",
      analysis: { sentiment: "neutral", summary: "Nothing to say today." },
    };
    render(<AiPanel />);
    await screen.findByText("Nothing to say today.");

    expect(screen.queryByTestId("price-target")).toBeNull();
  });
});
