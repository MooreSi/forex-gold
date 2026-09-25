/**
 * The Remote tab is the only window this machine has onto a headless VPS.
 *
 * Two things the NiceGUI page did and the React port did not (2026-09-23):
 *
 * * **It kept the link state live.** The page re-read it every 2 s. The port
 *   read it once, on mount, so "Save and connect" left the tab saying
 *   "connecting" for ever -- the operator could not tell a pair that had come
 *   up from one that never would.
 * * **It showed the VPS's health**: CPU, memory, which engines were running.
 *   On a headless VPS there is no other screen that says any of it.
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RemoteTab } from "../tabs/RemoteTab";

const STATE = {
  server: { enabled: false, port: 8765, running: false, fingerprint: "", token_set: false },
  client: {
    host: "10.0.0.5", port: 8765, token_set: true,
    conn_state: "connected", last_error: "",
    remote_status: {
      balance: 1000, equity: 990, open_positions: [], active_trader: "remote_vps",
      engines: { breakout: true, reversal_engine: false },
      ea_connected: true,
      cpu_percent: 91, mem_used_mb: 3000, mem_total_mb: 4000, mem_percent: 75,
    },
  },
  headless: false,
  centralized_signal_gen: false,
};

let responses: unknown[];

beforeEach(() => {
  responses = [];
  vi.stubGlobal("fetch", vi.fn(async () => {
    const body = responses.length > 1 ? responses.shift() : responses[0];
    return { ok: true, status: 200, json: async () => body };
  }));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the remote node's health", () => {
  it("shows its CPU and memory, and flags a CPU that is nearly flat out", async () => {
    responses = [STATE];
    render(<RemoteTab />);

    const health = await screen.findByTestId("remote-health");
    expect(health).toHaveTextContent("CPU 91%");
    expect(health).toHaveTextContent("3,000 / 4,000 MB");
    expect(screen.getByTestId("remote-cpu")).toHaveClass("text-loss");
  });

  it("says which of its engines are running", async () => {
    responses = [STATE];
    render(<RemoteTab />);

    const engines = await screen.findByTestId("remote-engines");
    expect(engines).toHaveTextContent(/breakout on/i);
    expect(engines).toHaveTextContent(/reversal engine off/i);
  });

  it("says whether its EA is connected", async () => {
    responses = [STATE];
    render(<RemoteTab />);

    expect(await screen.findByTestId("remote-health")).toHaveTextContent("EA connected");
  });

  it("shows none of it while the link is down", async () => {
    responses = [{ ...STATE, client: { ...STATE.client, conn_state: "disconnected",
                                       remote_status: {} } }];
    render(<RemoteTab />);

    await screen.findByLabelText("VPS address");
    expect(screen.queryByTestId("remote-health")).not.toBeInTheDocument();
  });
});

describe("the link state", () => {
  it("is re-read while the tab is open, so a pair coming up is seen", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const connecting = { ...STATE, client: { ...STATE.client, conn_state: "connecting",
                                              remote_status: {} } };
    responses = [connecting, STATE];
    render(<RemoteTab />);

    expect(await screen.findByText("connecting")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3_500); });

    expect(await screen.findByTestId("remote-health")).toBeInTheDocument();
  });
});

describe("reconnecting", () => {
  it("says a stored token is used when the box is left blank", async () => {
    responses = [{ ...STATE, client: { ...STATE.client, conn_state: "disconnected",
                                       remote_status: {} } }];
    render(<RemoteTab />);

    await screen.findByLabelText("Shared token");
    expect(screen.getByText(/leave blank to use it/i)).toBeInTheDocument();
    expect(screen.queryByText(/re-enter it/i)).not.toBeInTheDocument();
  });
});
