import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TradingStatusBadge } from "../TradingStatusBadge";
import { api } from "@/api/client";
import { resetPolls } from "@/hooks/usePoll";

const OK = {
  state: "ok", label: "Circuit Breaker OK",
  detail: "Nothing is holding automated entries.",
  until: null, resume_ts: null, can_resume: false,
};

const HALTED = {
  state: "halted", label: "Trading Paused until 20 Sep 09:00",
  detail: "Circuit breaker active (3 consecutive losses)",
  until: 1, resume_ts: null, can_resume: true,
};

function mockBadge(body: unknown) {
  return vi.spyOn(api, "get").mockResolvedValue(body as never);
}

describe("TradingStatusBadge", () => {
  // `usePoll` keys its cache by string in a module-level registry, so without
  // this every test after the first renders the FIRST test's payload and
  // passes or fails for reasons that have nothing to do with it.
  beforeEach(() => {
    resetPolls();
    vi.restoreAllMocks();
  });
  afterEach(() => {
    resetPolls();
    vi.restoreAllMocks();
  });

  it("shows the all-clear when nothing is holding entries", async () => {
    mockBadge(OK);
    render(<TradingStatusBadge />);

    expect(await screen.findByText("Circuit Breaker OK")).toBeInTheDocument();
  });

  it("shows the halt and when it ends", async () => {
    mockBadge(HALTED);
    render(<TradingStatusBadge />);

    expect(await screen.findByText("Trading Paused until 20 Sep 09:00"))
      .toBeInTheDocument();
  });

  it("renders whatever the backend says rather than deciding itself", async () => {
    // A second opinion about a risk state produces two answers that drift.
    // Given a blackout, this must say blackout even though no news data
    // reached the component.
    mockBadge({
      state: "news_blackout", label: "News Blackout",
      detail: "Non-farm payrolls", until: null,
      resume_ts: Date.now() / 1000 + 300, can_resume: false,
    });
    render(<TradingStatusBadge />);

    expect(await screen.findByText(/News Blackout/)).toBeInTheDocument();
  });

  it("offers a Resume when one would do something", async () => {
    mockBadge(HALTED);
    render(<TradingStatusBadge />);
    await screen.findByText(HALTED.label);

    await userEvent.click(screen.getByTestId("trading-status-badge"));

    expect(await screen.findByText("Resume Trading")).toBeInTheDocument();
  });

  it("offers the pause form, not a Resume, for a blackout that lifts itself", async () => {
    mockBadge({
      state: "news_blackout", label: "News Blackout", detail: "Non-farm payrolls",
      until: null, resume_ts: null, can_resume: false,
    });
    render(<TradingStatusBadge />);
    await screen.findByText(/News Blackout/);

    await userEvent.click(screen.getByTestId("trading-status-badge"));

    // The dialog still opens — a blackout is no reason to lose the ability
    // to stop trading by hand — but nothing offers to lift what lifts itself.
    expect(await screen.findByTestId("trading-status-dialog")).toBeInTheDocument();
    expect(screen.queryByText("Resume Trading")).not.toBeInTheDocument();
  });

  it("does not resume until the confirm is pressed", async () => {
    mockBadge(HALTED);
    const post = vi.spyOn(api, "post").mockResolvedValue({} as never);
    render(<TradingStatusBadge />);
    await screen.findByText(HALTED.label);

    await userEvent.click(screen.getByTestId("trading-status-badge"));

    expect(post).not.toHaveBeenCalled();
  });

  it("resumes through the endpoint that clears every hold", async () => {
    mockBadge(HALTED);
    const post = vi.spyOn(api, "post").mockResolvedValue({} as never);
    render(<TradingStatusBadge />);
    await screen.findByText(HALTED.label);
    await userEvent.click(screen.getByTestId("trading-status-badge"));

    await userEvent.click(await screen.findByText("Resume Trading"));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/api/trading/resume-all"));
  });

  it("says nothing at all rather than guessing before the first read", () => {
    vi.spyOn(api, "get").mockReturnValue(new Promise(() => {}) as never);

    const { container } = render(<TradingStatusBadge />);

    // An "OK" rendered from no data is a false all-clear, which is the one
    // thing this badge must never show.
    expect(container).toBeEmptyDOMElement();
  });
});
