import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OrbSection } from "../internal/OrbSection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The London opening-range breakout card.
 *
 * Restored 2026-09-18 — the NiceGUI Trading page had it and the React port
 * dropped it, so the report, its chart, the Execute button, the lot size and
 * the unattended auto-execute were all unreachable.
 *
 * Execute opens a real position, so the questions here are: can it be
 * triggered by accident, does it name the numbers first, and are the numbers
 * it sends the ones that were on screen.
 *
 * Nothing here reaches a broker: `fetch` is a recorder.
 */
const REPORT = {
  direction: "bullish", phase: "confirmed", current_price: 2401.5,
  asia_low: 2380, asia_high: 2395, asia_range: 15,
  or_low: 2396, or_high: 2400, or_range: 4,
  stop: 2398, target: 2405, target2: 2408, rr: 2,
  position_note: "above both ranges",
};

let state: Record<string, unknown>;
let response: { status: number; body: unknown };
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetPolls();
  state = {
    report: { ...REPORT }, chart_png_base64: "UE5H",
    lot_size: 0, auto_execute: false, control_target: "local",
  };
  response = { status: 200, body: { mt5_ticket: 7, entry_price: 2401, where: "local" } };
  fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") {
      return { ok: response.status < 400, status: response.status,
               json: async () => response.body };
    }
    return { ok: true, status: 200, json: async () => state };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { resetPolls(); vi.unstubAllGlobals(); });

const writes = () =>
  fetchMock.mock.calls.filter((c) => c[1]?.method && c[1].method !== "GET");

describe("the report", () => {
  it("shows both ranges and the breakout state", async () => {
    render(<OrbSection />);

    expect(await screen.findByText(/BREAKOUT — BULLISH/)).toBeInTheDocument();
    // The card labels, not the prose underneath — "London opening range" also
    // appears in the sentence explaining where the stop comes from.
    expect(screen.getByText("Asian range (00:00–08:00 UTC)")).toBeInTheDocument();
    expect(screen.getByText("London opening range (08:00–08:15 UTC)")).toBeInTheDocument();
  });

  it("shows the setup with the stop, both targets and the R:R", async () => {
    render(<OrbSection />);
    await screen.findByText(/Breakout setup/);

    expect(screen.getByText("Stop")).toBeInTheDocument();
    expect(screen.getByText(/Target 2 \(3:1, info only\)/)).toBeInTheDocument();
    expect(screen.getByText("2.00:1")).toBeInTheDocument();
  });

  it("says the 3:1 level is not traded", async () => {
    // The automated path closes fully at the target. Showing a second target
    // without saying so invites the operator to expect a runner.
    render(<OrbSection />);

    expect(await screen.findByText(/does not manage a partial-close ladder/))
      .toBeInTheDocument();
  });

  it("renders the backend's chart rather than drawing its own", async () => {
    render(<OrbSection />);

    const img = await screen.findByAltText("ORB chart");
    expect(img).toHaveAttribute("src", "data:image/png;base64,UE5H");
  });

  it("survives a chart the backend could not render", async () => {
    // The numbers are the point; the picture is not.
    state.chart_png_base64 = null;
    render(<OrbSection />);

    expect(await screen.findByText(/BREAKOUT — BULLISH/)).toBeInTheDocument();
    expect(screen.queryByAltText("ORB chart")).not.toBeInTheDocument();
  });

  it("says there is no report yet rather than showing an empty card", async () => {
    state.report = null;
    render(<OrbSection />);

    expect(await screen.findByText(/No ORB report available yet/)).toBeInTheDocument();
  });

  it("offers no Execute button until a breakout is confirmed", async () => {
    state.report = { ...REPORT, direction: "unconfirmed" };
    render(<OrbSection />);
    await screen.findByText(/UNCONFIRMED/);

    expect(screen.queryByRole("button", { name: /Execute/ })).not.toBeInTheDocument();
  });

  it("hides the opening range while it is still forming", async () => {
    state.report = { ...REPORT, phase: "forming", direction: "inside" };
    render(<OrbSection />);
    await screen.findByText("Asian range (00:00–08:00 UTC)");

    expect(screen.queryByText("London opening range (08:00–08:15 UTC)"))
      .not.toBeInTheDocument();
  });
});

describe("executing", () => {
  it("does not open anything on the first press", async () => {
    render(<OrbSection />);

    await userEvent.click(await screen.findByRole("button", { name: /Execute BUY/ }));

    expect(writes()).toHaveLength(0);
  });

  it("names the numbers before it acts", async () => {
    render(<OrbSection />);
    await userEvent.click(await screen.findByRole("button", { name: /Execute BUY/ }));

    expect(screen.getByText(/stop 2398\.00 · target 2405\.00/)).toBeInTheDocument();
    expect(screen.getByText(/real position/)).toBeInTheDocument();
  });

  it("sends the numbers that were on screen, not a fresh read", async () => {
    // The report moves as price does. Re-reading it on the way to the broker
    // would trade numbers that were never shown.
    render(<OrbSection />);
    await userEvent.click(await screen.findByRole("button", { name: /Execute BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: "Open BUY" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/trading/orb/execute");
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      direction: "BUY", stop_loss: 2398, take_profit: 2405,
    });
  });

  it("sends SELL for a bearish breakout", async () => {
    // Negative control: a card that always sent BUY would pass the test above
    // and trade the wrong way on every bearish morning.
    state.report = { ...REPORT, direction: "bearish" };
    render(<OrbSection />);
    await userEvent.click(await screen.findByRole("button", { name: /Execute SELL/ }));
    await userEvent.click(screen.getByRole("button", { name: "Open SELL" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body).direction).toBe("SELL");
  });

  it("says where the order landed when it was the remote node", async () => {
    response = { status: 200, body: { mt5_ticket: 9, entry_price: 2401, where: "remote" } };
    render(<OrbSection />);
    await userEvent.click(await screen.findByRole("button", { name: /Execute BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: "Open BUY" }));

    expect(await screen.findByRole("status")).toHaveTextContent("on the remote node");
  });

  it("shows a refusal in the backend's own words", async () => {
    response = {
      status: 409,
      body: { error: { kind: "refusal",
                       message: "Trading stood down — the VPS is the active trader.",
                       ref: null } },
    };
    render(<OrbSection />);
    await userEvent.click(await screen.findByRole("button", { name: /Execute BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: "Open BUY" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("stood down");
  });

  it("warns when the order would land on the other machine", async () => {
    state.control_target = "remote";
    render(<OrbSection />);
    await userEvent.click(await screen.findByRole("button", { name: /Execute BUY/ }));

    expect(screen.getByText(/on the remote node/)).toBeInTheDocument();
  });
});

describe("the settings", () => {
  it("saves a lot size when the field is left", async () => {
    render(<OrbSection />);
    const lots = await screen.findByLabelText("ORB lot size");

    await userEvent.clear(lots);
    await userEvent.type(lots, "0.05");
    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ lot_size: 0.05 });
  });

  it("says what a lot size of zero means", async () => {
    render(<OrbSection />);

    expect(await screen.findByText(/sizes it from your risk %/)).toBeInTheDocument();
  });

  it("saves the unattended auto-execute", async () => {
    render(<OrbSection />);

    await userEvent.click(await screen.findByLabelText(
      "Auto-execute this setup every morning (unattended)"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ auto_execute: true });
  });

  it("says what unattended means, including on a paired node", async () => {
    // "Places this trade automatically" with no mention of the other machine
    // is how somebody ends up with two.
    render(<OrbSection />);

    expect(await screen.findByText(/no double trade/)).toBeInTheDocument();
  });
});
