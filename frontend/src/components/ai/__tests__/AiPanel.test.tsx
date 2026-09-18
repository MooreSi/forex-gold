import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AiPanel } from "../AiPanel";

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

const EVIDENCE = {
  evidence: [
    {
      channel_name: "GoldSignals",
      stats: {
        total_signals: 40, closed_trades: 30, win_rate_pct: 61.0,
        total_pnl: 310.5, phantom_tp_count: 2,
      },
    },
  ],
};

let fetchMock: ReturnType<typeof vi.fn>;
let configured: boolean;

beforeEach(() => {
  configured = true;
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/ai/subjects") {
      return { ok: true, status: 200, json: async () => ({ ...SUBJECTS, configured }) };
    }
    if (url.startsWith("/api/ai/evidence")) {
      return { ok: true, status: 200, json: async () => EVIDENCE };
    }
    if (url === "/api/ai/analyse" && init?.method === "POST") {
      return {
        ok: true, status: 200,
        json: async () => ({ answer: "Two channels are claiming TPs they did not hit." }),
      };
    }
    return { ok: false, status: 404, statusText: "", json: async () => ({}) };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const called = (prefix: string) =>
  fetchMock.mock.calls.filter((c) => String(c[0]).startsWith(prefix));

describe("the free half", () => {
  it("shows the measured numbers without asking a model", async () => {
    // The numbers are the answer most of the time. A page that could only show
    // them by asking a model would make every glance billable.
    render(<AiPanel />);

    expect(await screen.findByText("GoldSignals")).toBeInTheDocument();
    expect(screen.getByText("61.0%")).toBeInTheDocument();
    expect(screen.getByText("+$310.50")).toBeInTheDocument();
    expect(called("/api/ai/analyse")).toHaveLength(0);
  });

  it("surfaces phantom TPs, which is the number the tab exists for", async () => {
    render(<AiPanel />);

    await screen.findByText("GoldSignals");
    expect(screen.getByText("Phantom TPs")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("re-reads the evidence when the subject changes, still without a model", async () => {
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    await userEvent.click(screen.getByRole("button", { name: "Fixed strategies vs DPM" }));

    await waitFor(() => {
      expect(called("/api/ai/evidence").some((c) =>
        String(c[0]).includes("subject=strategies"))).toBe(true);
    });
    expect(called("/api/ai/analyse")).toHaveLength(0);
  });

  it("re-reads when the window changes", async () => {
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    await userEvent.click(screen.getByRole("button", { name: "90d" }));

    await waitFor(() => {
      expect(called("/api/ai/evidence").some((c) =>
        String(c[0]).includes("days=90"))).toBe(true);
    });
  });
});

describe("the billable half", () => {
  it("says which provider is about to be charged", async () => {
    render(<AiPanel />);

    expect(await screen.findByText(/billed by anthropic/)).toBeInTheDocument();
    expect(screen.getByText(/The numbers above are free/)).toBeInTheDocument();
  });

  it("names the model in the header so it is not a surprise", async () => {
    render(<AiPanel />);

    expect(await screen.findByText("anthropic · claude-opus-5")).toBeInTheDocument();
  });

  it("only calls the model when the button is pressed", async () => {
    render(<AiPanel />);
    await screen.findByText("GoldSignals");
    expect(called("/api/ai/analyse")).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: /Ask the model/ }));

    await waitFor(() => expect(called("/api/ai/analyse")).toHaveLength(1));
    expect(JSON.parse(called("/api/ai/analyse")[0][1].body)).toEqual({
      subject: "channels", days: 30,
    });
  });

  it("shows the answer", async () => {
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    await userEvent.click(screen.getByRole("button", { name: /Ask the model/ }));

    expect(await screen.findByTestId("ai-answer")).toHaveTextContent(
      "Two channels are claiming TPs they did not hit.",
    );
  });

  it("disables the button with a reason when no provider is configured", async () => {
    // Not a silent no-op: an empty answer reads as a model with no opinion.
    configured = false;
    render(<AiPanel />);

    const ask = await screen.findByRole("button", { name: /Ask the model/ });
    expect(ask).toBeDisabled();
    expect(ask).toHaveAttribute("title", expect.stringContaining("Settings → AI"));
  });

  it("shows a refusal from the backend verbatim", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/ai/subjects") {
        return { ok: true, status: 200, json: async () => ({ ...SUBJECTS, configured: true }) };
      }
      if (url.startsWith("/api/ai/evidence")) {
        return { ok: true, status: 200, json: async () => EVIDENCE };
      }
      if (init?.method === "POST") {
        return {
          ok: false, status: 409, statusText: "",
          json: async () => ({
            error: { kind: "refusal", message: "The provider rejected the API key.", ref: null },
          }),
        };
      }
      return { ok: false, status: 404, statusText: "", json: async () => ({}) };
    });
    render(<AiPanel />);
    await screen.findByText("GoldSignals");

    await userEvent.click(screen.getByRole("button", { name: /Ask the model/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The provider rejected the API key.",
    );
  });

  it("drops a stale answer when the subject changes", async () => {
    // An answer about the channels sitting under a strategies table is the
    // kind of thing somebody acts on.
    render(<AiPanel />);
    await screen.findByText("GoldSignals");
    await userEvent.click(screen.getByRole("button", { name: /Ask the model/ }));
    await screen.findByTestId("ai-answer");

    await userEvent.click(screen.getByRole("button", { name: "Fixed strategies vs DPM" }));

    await waitFor(() => expect(screen.queryByTestId("ai-answer")).toBeNull());
  });
});
