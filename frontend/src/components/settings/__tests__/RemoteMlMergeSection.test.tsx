import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RemoteMlMergeSection } from "../internal/RemoteMlMergeSection";

/**
 * Merge training data (docs/todo/026). Each node keeps the other's labelled
 * rows apart and learns from them only if they score at least as well on the
 * newest signals. A kept merge changes what the Reversal gate lets through,
 * so it never starts on one press, and the result says per node and per
 * model what was decided.
 *
 * Nothing here merges anything: `fetch` is a recorder.
 */
let fetchMock: ReturnType<typeof vi.fn>;
let status: Record<string, unknown>;

const idle = {
  running: false, stage: "", error: null, result: null,
  datasets: { re: { imported: 0, decision: null }, bo: { imported: 0, decision: null },
              pro: { imported: 0, decision: null } },
};

beforeEach(() => {
  status = idle;
  fetchMock = vi.fn(async (_url: string, init?: RequestInit) => ({
    ok: true, status: 200,
    json: async () => (init?.method === "POST" ? { note: "Merge started." } : status),
  }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const posts = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "POST");

describe("RemoteMlMergeSection", () => {
  it("sends nothing on the first press", async () => {
    render(<RemoteMlMergeSection connected />);

    await userEvent.click(screen.getByRole("button", { name: /Merge training data/ }));

    expect(posts()).toHaveLength(0);
    expect(screen.getByText(/scores at least as well/)).toBeTruthy();
  });

  it("starts the merge once confirmed", async () => {
    render(<RemoteMlMergeSection connected />);
    await userEvent.click(screen.getByRole("button", { name: /Merge training data/ }));

    await userEvent.click(screen.getByRole("button", { name: /^Merge both ways/ }));

    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0][0]).toBe("/api/remote/ml-merge");
  });

  it("shows what each node decided for each model", async () => {
    status = {
      ...idle,
      result: {
        pulled: {}, pushed: {},
        local: { re: { use: true, a: 0.01, b: 0.04, n_new: 1440, reason: "merged history scores at least as well" } },
        remote: { re: { use: false, a: 0.05, b: 0.02, n_new: 5746, reason: "merged history scores worse" } },
      },
    };

    render(<RemoteMlMergeSection connected />);

    expect(await screen.findByText(/This Mac/)).toBeTruthy();
    expect(screen.getByText(/Learns from the merged history/)).toBeTruthy();
    expect(screen.getByText(/Kept apart/)).toBeTruthy();
  });

  it("says why a merge failed", async () => {
    status = { ...idle, error: "The VPS is running older code that cannot merge." };

    render(<RemoteMlMergeSection connected />);

    expect(await screen.findByText(/older code/)).toBeTruthy();
  });

  it("cannot be pressed without a link", () => {
    render(<RemoteMlMergeSection connected={false} />);

    expect(screen.getByRole("button", { name: /Merge training data/ })).toHaveProperty("disabled", true);
  });
});
