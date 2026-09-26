/**
 * Settings > Latency (docs/todo/006).
 *
 * What matters on this screen is that it never makes a delay look smaller than
 * it is: an unmeasured hop reads "—", not "0 ms"; a probe that did not answer
 * is red with its reason; a slow hop is amber. And the VPS section appears
 * exactly when a VPS is paired.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LatencyTab } from "../tabs/LatencyTab";

const hop = (id: string, label: string, stats: Record<string, number> = {}, slow = false) => ({
  id, label, detail: "", amber_ms: 1000, stats, slow,
});

function passive(paired: boolean) {
  return {
    paired,
    structural: [{ id: "breakout_cycle", label: "Breakout analysis cycle", seconds: 60, detail: "d" }],
    pipelines: {
      telegram: {
        hops: [
          hop("decide", "Parse + decide", { n: 3, p50: 40, p90: 60, max: 70 }),
          hop("order", "Order: gates, sizing, EA/bridge, broker, ack", { n: 3, p50: 900, p90: 8200, max: 9000 }, true),
          hop("delivery", "Telegram post -> this app"),
        ],
        recent: [{ key: "1", label: "GOLD VIP", at: 1_790_000_000, hops: { decide: 40, order: 8200, delivery: null } }],
      },
      engine: { hops: [hop("order", "Execution")], recent: [] },
      forwarded: { hops: [], recent: [] },
    },
  };
}

const CHECK = {
  local: {
    probes: {
      telegram: { ok: true, ms: 120, detail: "", amber_ms: 1000, session_dc: 4, nearest_dc: 4 },
      ea: { ok: false, ms: null, detail: "EA not connected", amber_ms: 600 },
      bridge: { ok: true, ms: 900, detail: "", amber_ms: 250 },
    },
    broker: { available: true, days: 7, servers: {
      "Broker-Demo": { n: 742, median_ms: 218, p90_ms: 12575, max_ms: 84903, over_5s: 145, slow: true },
    } },
  },
  vps: { ok: true, rtt_ms: 35, detail: "", amber_ms: 300,
    remote: { probes: { ea: { ok: true, ms: 210, detail: "", amber_ms: 600 } },
      pipelines: { forwarded: { hops: [hop("order", "Forwarded order: received -> executed", { n: 2, p50: 400, p90: 500, max: 500 })], recent: [] } } } },
};

let posts: string[] = [];

function serve(paired: boolean) {
  posts = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const isPost = init?.method === "POST";
    if (isPost) posts.push(String(url));
    return { ok: true, status: 200, json: async () => (isPost ? CHECK : passive(paired)) };
  }));
}

afterEach(() => vi.unstubAllGlobals());

describe("before any check", () => {
  beforeEach(() => serve(false));

  it("shows both checkers", async () => {
    render(<LatencyTab />);

    expect(await screen.findByTestId("checker-telegram")).toBeInTheDocument();
    expect(screen.getByTestId("checker-engine")).toBeInTheDocument();
  });

  it("reads an unmeasured hop as a dash, never as zero", async () => {
    render(<LatencyTab />);

    const row = await screen.findByTestId("hop-delivery");
    expect(row).toHaveAttribute("data-state", "none");
    expect(row).toHaveTextContent("—");
    expect(row).not.toHaveTextContent("0 ms");
  });

  it("marks a slow hop", async () => {
    render(<LatencyTab />);

    const row = within(await screen.findByTestId("hops-telegram")).getByTestId("hop-order");
    expect(row).toHaveAttribute("data-state", "slow");
    expect(row).toHaveTextContent("8.20 s");
  });

  it("states the engine's polling wait", async () => {
    render(<LatencyTab />);

    expect(await screen.findByTestId("wait-breakout_cycle")).toHaveTextContent("every 60 s");
  });

  it("has no VPS section when nothing is paired", async () => {
    render(<LatencyTab />);
    await screen.findByTestId("checker-telegram");

    expect(screen.queryByText("VPS")).not.toBeInTheDocument();
  });

  it("runs no probe until asked", async () => {
    render(<LatencyTab />);
    await screen.findByTestId("checker-telegram");

    expect(posts).toEqual([]);
  });
});

describe("Run check", () => {
  it("shows each probe, a dead one in red with its reason", async () => {
    serve(false);
    render(<LatencyTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Run check" }));

    expect(posts).toEqual(["/api/settings/latency/check"]);
    const probes = await screen.findByTestId("probes-telegram");
    const ea = within(probes).getByTestId("hop-probe-ea");
    expect(ea).toHaveAttribute("data-state", "fail");
    expect(ea).toHaveTextContent("EA not connected");
    expect(within(probes).getByTestId("hop-probe-bridge")).toHaveAttribute("data-state", "slow");
    expect(within(probes).getByTestId("hop-broker-Broker-Demo")).toHaveTextContent("145 took over 5 s");
  });

  it("includes the VPS when one is paired", async () => {
    serve(true);
    render(<LatencyTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Run check" }));

    const vps = await screen.findByTestId("vps-telegram");
    expect(within(vps).getByTestId("hop-vps-link")).toHaveTextContent("35 ms");
    expect(within(vps).getByTestId("hop-VPS: probe-ea")).toHaveTextContent("210 ms");
    expect(within(vps).getByTestId("hop-VPS: order")).toHaveTextContent("400 ms");
  });
});
