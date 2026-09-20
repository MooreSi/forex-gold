/**
 * Expert tunables — the catalogue, rendered as the catalogue really is.
 *
 * The endpoint returns `{ "<group name>": [ {key,label,value,default,min,max,
 * unit,desc}, ... ] }`. The tab read it as `{ "<key>": {value,default} }`, so
 * against the running app it rendered FOUR empty boxes labelled "Risk
 * filters", "Instant Market Entry", "Signal handling" and "Broker
 * reconciliation" — the group names — and all thirteen real tunables were
 * invisible and uneditable. Verified in the browser on 2026-09-20.
 *
 * The test fixture agreed with the component (`{ re_min_adx: {...} }`, a shape
 * the endpoint has never returned), which is why nothing caught it.
 *
 * "Reset all to defaults" POSTed to `/api/settings/expert-params`, which has
 * no POST handler — a 405 every time. The reset endpoint is
 * `/api/settings/expert-params/reset`.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TunablesTab } from "../tabs/TunablesTab";

// The real payload, trimmed to two groups.
const CATALOGUE = {
  "Risk filters": [
    {
      key: "min_tp1_rr", label: "Minimum TP1 reward:risk", value: 0.75,
      default: 0.75, min: 0.1, max: 5.0, unit: "R",
      desc: "A signal whose TP1 is closer than this is skipped.",
    },
    {
      key: "max_spread_pts", label: "Maximum spread", value: 45,
      default: 40, min: 5, max: 200, unit: "pt",
      desc: "Entries are refused above this spread.",
    },
  ],
  "Signal handling": [
    {
      key: "pending_signal_expiry_s", label: "Queued signal expiry", value: 120,
      default: 120, min: 15, max: 3600, unit: "s",
      desc: "A signal queued longer than this is dropped.",
    },
  ],
};

let fetchMock: ReturnType<typeof vi.fn>;
const calls: { url: string; method: string; body: unknown }[] = [];

beforeEach(() => {
  calls.length = 0;
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({
      url, method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : null,
    });
    return { ok: true, status: 200, json: async () => CATALOGUE };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("the catalogue", () => {
  it("shows every tunable, not the group names", async () => {
    render(<TunablesTab />);

    expect(await screen.findByText("Minimum TP1 reward:risk")).toBeInTheDocument();
    expect(screen.getByText("Maximum spread")).toBeInTheDocument();
    expect(screen.getByText("Queued signal expiry")).toBeInTheDocument();
  });

  it("groups them under the headings the catalogue gives", async () => {
    render(<TunablesTab />);

    expect(await screen.findByRole("heading", { name: "Risk filters" }))
      .toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Signal handling" }))
      .toBeInTheDocument();
  });

  it("shows the value actually in force", async () => {
    render(<TunablesTab />);

    expect(await screen.findByLabelText("Minimum TP1 reward:risk"))
      .toHaveValue("0.75");
  });

  it("explains what each one does", async () => {
    // A number with no explanation is a number nobody dares change.
    render(<TunablesTab />);

    expect(await screen.findByText(/A signal whose TP1 is closer/))
      .toBeInTheDocument();
  });

  it("says the default and the range, so a change can be judged", async () => {
    render(<TunablesTab />);

    const hint = await screen.findByTestId("tunable-hint-max_spread_pts");
    expect(hint).toHaveTextContent("default 40");
    expect(hint).toHaveTextContent("5");
    expect(hint).toHaveTextContent("200");
  });

  it("marks a value that has been moved off its default", async () => {
    // max_spread_pts is 45 against a default of 40; min_tp1_rr is unchanged.
    render(<TunablesTab />);

    await screen.findByText("Maximum spread");
    expect(screen.getByTestId("tunable-max_spread_pts"))
      .toHaveAttribute("data-modified", "true");
    expect(screen.getByTestId("tunable-min_tp1_rr"))
      .toHaveAttribute("data-modified", "false");
  });
});

describe("saving", () => {
  it("writes one tunable by its key, not by its label", async () => {
    render(<TunablesTab />);
    const field = await screen.findByLabelText("Maximum spread");

    await userEvent.clear(field);
    await userEvent.type(field, "60");
    await userEvent.tab();

    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toContain("/api/settings/expert-params");
    expect(put.body).toEqual({ values: { max_spread_pts: 60 } });
  });
});

describe("resetting", () => {
  it("posts to the reset endpoint, not to the catalogue", async () => {
    // It posted to /expert-params, which has no POST handler: 405 every time.
    render(<TunablesTab />);
    await screen.findByText("Maximum spread");

    await userEvent.click(screen.getByRole("button", { name: /Reset all/ }));

    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toContain("/api/settings/expert-params/reset");
  });

  it("can reset a single tunable by key", async () => {
    render(<TunablesTab />);
    await screen.findByText("Maximum spread");

    await userEvent.click(screen.getByTestId("tunable-reset-max_spread_pts"));

    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toContain("/api/settings/expert-params/reset");
    expect(post.body).toEqual({ key: "max_spread_pts" });
  });

  it("offers no per-tunable reset for a value already at its default", async () => {
    render(<TunablesTab />);
    await screen.findByText("Minimum TP1 reward:risk");

    expect(screen.queryByTestId("tunable-reset-min_tp1_rr")).not.toBeInTheDocument();
  });
});
