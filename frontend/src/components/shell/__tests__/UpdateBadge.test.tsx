/**
 * The header's UPDATE AVAILABLE badge and its popup.
 *
 * The NiceGUI header flashed a green badge as soon as a fetch found commits
 * on origin this checkout did not have, and clicking it opened a popup saying
 * in plain English what the update changed, with an Update Now button in it.
 * The React port shipped neither; the only place an update surfaced was
 * Settings > Node & Updates. Asked for on 2026-09-20.
 *
 * Two rules worth pinning. The badge must not appear when there is nothing to
 * install -- an operator who clicks it and finds nothing stops trusting it.
 * And the popup must still offer the update when the plain-English summary
 * could not be produced: the summary costs a paid model call and is a nicety,
 * never a precondition (the service's own docstring says so).
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UpdateBadge } from "../UpdateBadge";

const AVAILABLE = { available: true, commits: 3, remote_sha: "bbbbbbb2" };

let statusBody: Record<string, unknown>;
let applyBody: Record<string, unknown>;
let posted: string[];

beforeEach(() => {
  posted = [];
  applyBody = { result: { ok: true } };
  statusBody = {
    current: "0.5",
    update: {
      available: true, local_sha: "aaaaaaa1", remote_sha: "bbbbbbb2",
      commits: [
        { sha: "b1", short_sha: "b1b1b1b", summary: "Fix the chart crash" },
        { sha: "b2", short_sha: "b2b2b2b", summary: "Port the log export" },
      ],
      error: null,
    },
    changes: ["Charts no longer crash when you switch tabs"],
    changes_error: "",
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (init?.method === "POST") {
      posted.push(u);
      return { ok: true, status: 200, json: async () => applyBody };
    }
    return { ok: true, status: 200, json: async () => statusBody };
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("the badge", () => {
  it("is not rendered when this install is up to date", () => {
    render(<UpdateBadge update={{ available: false, commits: 0, remote_sha: "" }} />);

    expect(screen.queryByRole("button", { name: /update available/i })).toBeNull();
  });

  it("is not rendered when the header has not said yet", () => {
    render(<UpdateBadge update={null} />);

    expect(screen.queryByRole("button", { name: /update available/i })).toBeNull();
  });

  it("appears when commits are waiting on GitHub", () => {
    render(<UpdateBadge update={AVAILABLE} />);

    expect(screen.getByRole("button", { name: /update available/i })).toBeInTheDocument();
  });
});

describe("the popup", () => {
  it("does not fetch the detail until it is opened", () => {
    // Opening costs a model call for the summary. Paying it for a badge
    // nobody clicked is money spent on text nobody reads.
    render(<UpdateBadge update={AVAILABLE} />);

    expect(fetch).not.toHaveBeenCalled();
  });

  it("lists the plain-English summary when there is one", async () => {
    render(<UpdateBadge update={AVAILABLE} />);
    await userEvent.click(screen.getByRole("button", { name: /update available/i }));

    expect(
      await screen.findByText(/Charts no longer crash/),
    ).toBeInTheDocument();
  });

  it("falls back to the commit subjects when the summary failed", async () => {
    statusBody = {
      ...statusBody, changes: [],
      changes_error: "no AI provider configured (Settings > AI)",
    };
    render(<UpdateBadge update={AVAILABLE} />);
    await userEvent.click(screen.getByRole("button", { name: /update available/i }));

    expect(await screen.findByText(/Fix the chart crash/)).toBeInTheDocument();
  });

  it("still offers the update when the summary failed", async () => {
    // The rule from the service: a summary is never a precondition for
    // updating. An operator who cannot update because their AI provider is
    // unconfigured is worse off than one who updates without a description.
    statusBody = { ...statusBody, changes: [], changes_error: "model timed out" };
    render(<UpdateBadge update={AVAILABLE} />);
    await userEvent.click(screen.getByRole("button", { name: /update available/i }));

    const apply = await screen.findByRole("button", { name: /update now/i });
    expect(apply).not.toBeDisabled();
  });

  it("applies the update through the endpoint that applies it", async () => {
    // POST /api/node/update does not exist -- the route is /update/apply.
    // The Settings button posted to the former and silently did nothing.
    render(<UpdateBadge update={AVAILABLE} />);
    await userEvent.click(screen.getByRole("button", { name: /update available/i }));
    await userEvent.click(await screen.findByRole("button", { name: /update now/i }));

    await waitFor(() => expect(posted).toEqual(["/api/node/update/apply"]));
  });

  it("says so when applying the update failed", async () => {
    // Silent failure here is the worst outcome: the operator believes they
    // are on the new build and they are not.
    applyBody = { result: { ok: false, error: "git pull was refused" } };
    render(<UpdateBadge update={AVAILABLE} />);
    await userEvent.click(screen.getByRole("button", { name: /update available/i }));
    await userEvent.click(await screen.findByRole("button", { name: /update now/i }));

    expect(await screen.findByText(/git pull was refused/)).toBeInTheDocument();
  });
});
