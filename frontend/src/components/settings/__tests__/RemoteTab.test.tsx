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

/**
 * 2026-09-28, owner: "need a button to turn on/off the breakout and reversal
 * engine on the remote node page, also it still has bounce mentioned". The
 * VPS's Reversal Engine had been off for three days (a stand-down persisted
 * it as user-stopped) and this page was the only place that showed it, with
 * nothing to press.
 */
describe("the remote node's engines", () => {
  const withBounce = {
    ...STATE,
    client: { ...STATE.client, remote_status: {
      ...STATE.client.remote_status,
      engines: { breakout: true, bounce: false, reversal_engine: false },
    } },
  };

  it("never mentions Bounce, even from a VPS that still reports it", async () => {
    responses = [withBounce];
    render(<RemoteTab />);

    const engines = await screen.findByTestId("remote-engines");
    expect(engines).not.toHaveTextContent(/bounce/i);
    expect(screen.queryByRole("button", { name: /bounce/i })).not.toBeInTheDocument();
  });

  it("starts a stopped engine on the VPS", async () => {
    responses = [withBounce];
    render(<RemoteTab />);

    const button = await screen.findByRole("button", { name: "Start Reversal engine" });
    await act(async () => { button.click(); });

    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls;
    const post = calls.find(([url]) => url === "/api/engines/running");
    expect(post).toBeDefined();
    expect(JSON.parse(String(post![1]!.body))).toEqual({ engine: "reversal_engine", running: true });
  });

  it("stops a running engine on the VPS", async () => {
    responses = [withBounce];
    render(<RemoteTab />);

    const button = await screen.findByRole("button", { name: "Stop Breakout" });
    await act(async () => { button.click(); });

    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls;
    const post = calls.find(([url]) => url === "/api/engines/running");
    expect(JSON.parse(String(post![1]!.body))).toEqual({ engine: "breakout", running: false });
  });

  /**
   * 2026-09-29, owner: "i need the option in remote node to run the new trend
   * signal generator on the vps like the other engines". The VPS heartbeat
   * already carried `trend_pa` (running); the page's fixed list dropped it.
   */
  it("shows the Trend PA engine and stops it on the VPS", async () => {
    responses = [{ ...STATE, client: { ...STATE.client, remote_status: {
      ...STATE.client.remote_status,
      engines: { breakout: true, reversal_engine: false, trend_pa: true } } } }];
    render(<RemoteTab />);

    expect(await screen.findByTestId("remote-engines")).toHaveTextContent(/trend pa on/i);
    const button = screen.getByRole("button", { name: "Stop Trend PA" });
    await act(async () => { button.click(); });

    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls;
    const post = calls.find(([url]) => url === "/api/engines/running");
    expect(JSON.parse(String(post![1]!.body))).toEqual({ engine: "trend_pa", running: false });
  });

  it("offers no buttons while this machine is the trader", async () => {
    responses = [{ ...withBounce, client: { ...withBounce.client, remote_status: {
      ...withBounce.client.remote_status, active_trader: "local" } } }];
    render(<RemoteTab />);

    await screen.findByTestId("remote-engines");
    expect(screen.queryByRole("button", { name: /^(start|stop) /i })).not.toBeInTheDocument();
  });
});

describe("the remote node's open positions", () => {
  it("counts only positions the broker has, and names the rest", async () => {
    responses = [{ ...STATE, client: { ...STATE.client, remote_status: {
      ...STATE.client.remote_status,
      open_positions: [
        { trade_id: "f85f0bd3", mt5_ticket: 2103965158 },
        { trade_id: "1f5801a6", mt5_ticket: 0 },
        { trade_id: "3041d252", mt5_ticket: 0 },
      ],
    } } }];
    render(<RemoteTab />);

    const line = await screen.findByTestId("remote-open-positions");
    expect(line).toHaveTextContent("Open positions 1");
    expect(line).toHaveTextContent(/2 unconfirmed/i);
  });
});

/**
 * 2026-09-28, owner: "need to remove the (+2 unconfirmed ...) - these are
 * blocking 2 available slots". The VPS decides what goes, against its own
 * broker; this asks, after a confirmation, and shows its answer.
 */
describe("writing off the unconfirmed rows", () => {
  const withGhosts = { ...STATE, client: { ...STATE.client, remote_status: {
    ...STATE.client.remote_status,
    open_positions: [
      { trade_id: "1f5801a6", mt5_ticket: 0 },
      { trade_id: "3041d252", mt5_ticket: 0 },
    ],
  } } };

  function posts(url: string) {
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls;
    return calls.filter(([u, init]) => u === url && init?.method === "POST");
  }

  it("asks the VPS once the operator confirms", async () => {
    responses = [withGhosts];
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<RemoteTab />);

    const button = await screen.findByRole("button", { name: /write off/i });
    await act(async () => { button.click(); });

    expect(posts("/api/remote/write-off-unconfirmed")).toHaveLength(1);
  });

  it("sends nothing when the operator cancels", async () => {
    responses = [withGhosts];
    vi.stubGlobal("confirm", vi.fn(() => false));
    render(<RemoteTab />);

    const button = await screen.findByRole("button", { name: /write off/i });
    await act(async () => { button.click(); });

    expect(posts("/api/remote/write-off-unconfirmed")).toHaveLength(0);
  });

  it("is not offered when every position has a ticket", async () => {
    responses = [{ ...STATE, client: { ...STATE.client, remote_status: {
      ...STATE.client.remote_status,
      open_positions: [{ trade_id: "f85f0bd3", mt5_ticket: 2103965158 }],
    } } }];
    render(<RemoteTab />);

    await screen.findByTestId("remote-open-positions");
    expect(screen.queryByRole("button", { name: /write off/i })).not.toBeInTheDocument();
  });
});
