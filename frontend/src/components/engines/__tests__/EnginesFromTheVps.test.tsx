/**
 * Owner, 2026-10-21: with the VPS generating, the Signal Generator should show
 * "what the vps is doing so i can understand how the engine is developing".
 * The backend now answers the reports from the VPS and tags them
 * `node: "remote"`; the panel says so, reads the pro model from the report
 * (the VPS's) rather than /state (always this machine's), and says when the
 * VPS could not be reached rather than going quiet.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EnginesPanel } from "../EnginesPanel";
import { resetPolls } from "@/hooks/usePoll";

const STATE = {
  engines: [{ id: "reversal", label: "Reversal", running: false, built: true }],
  settings: {},
  control_target: "remote",
  pro_model: { ready: false, auc: null, n: 0, reason: "THIS MAC'S MODEL", corpus: {} },
};

let reversal: { status: number; body: unknown };
let breakout: { status: number; body: unknown };

beforeEach(() => {
  resetPolls();
  reversal = {
    status: 200,
    body: {
      node: "remote", realised: {}, edge: {}, shadow: [], history: [], ledger: [],
      pro_model: { ready: false, auc: null, n: 0, reason: "THE VPS'S MODEL", corpus: {} },
    },
  };
  breakout = { status: 200, body: { node: "remote", stats: {}, edge: {}, ml: {}, signals: [], log: [] } };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const r = url === "/api/engines/reversal/report" ? reversal
      : url === "/api/engines/breakout/report" ? breakout
      : { status: 200, body: url === "/api/engines/state" ? STATE : {} };
    return { ok: r.status < 400, status: r.status, json: async () => r.body };
  }));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("the reversal tab, with the VPS generating", () => {
  it("says the figures are the VPS's", async () => {
    render(<EnginesPanel />);
    expect(await screen.findByTestId("node-source")).toHaveTextContent(/VPS/);
  });

  it("shows the VPS's pro model, not this machine's", async () => {
    render(<EnginesPanel />);
    expect(await screen.findByText(/THE VPS'S MODEL/)).toBeInTheDocument();
    expect(screen.queryByText(/THIS MAC'S MODEL/)).not.toBeInTheDocument();
  });

  it("says when the VPS could not be reached", async () => {
    reversal = { status: 503, body: { error: { message: "The trading node could not be reached (x)." } } };
    render(<EnginesPanel />);
    expect(await screen.findByText(/could not be reached/)).toBeInTheDocument();
  });
});

describe("the breakout tab, with the VPS generating", () => {
  it("says the figures are the VPS's", async () => {
    render(<EnginesPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: /Breakout engine/ }));
    const panel = await screen.findByRole("tabpanel");
    expect(await within(panel).findByTestId("node-source")).toHaveTextContent(/VPS/);
  });
});
