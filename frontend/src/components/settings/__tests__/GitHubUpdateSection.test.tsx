/**
 * Settings > Node & Updates — the GitHub update card.
 *
 * Reported on 2026-09-20: it does not show which commit is installed against
 * what is on GitHub, and "Check for updates" appears to do nothing. Both were
 * real. The screen printed only the app version, and the check reloaded a
 * resource with no visible state of any kind, so a check that took as long as
 * a `git fetch` takes was indistinguishable from a dead button. The Apply
 * button posted to `/api/node/update`, which is not a route — the update it
 * claimed to start never ran.
 *
 * The NiceGUI card this replaces showed local commit, latest commit and a
 * status badge, which is what an operator needs to answer "am I on the
 * current build?".
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GitHubUpdateSection } from "../internal/GitHubUpdateSection";

const UP_TO_DATE = {
  current: "0.5",
  update: {
    available: false, local_sha: "aaaaaaa1111", remote_sha: "aaaaaaa1111",
    commits: [], error: null,
  },
  changes: [], changes_error: "",
};

const BEHIND = {
  current: "0.5",
  update: {
    available: true, local_sha: "aaaaaaa1111", remote_sha: "bbbbbbb2222",
    commits: [{ sha: "b1", short_sha: "b1b1b1b", summary: "Fix the chart crash" }],
    error: null,
  },
  changes: [], changes_error: "",
};

let body: Record<string, unknown>;
let requested: string[];
let gate: (() => void) | null;

beforeEach(() => {
  body = UP_TO_DATE;
  requested = [];
  gate = null;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    requested.push(`${init?.method ?? "GET"} ${url}`);
    if (gate) await new Promise<void>((r) => { gate = r; });
    return { ok: true, status: 200, json: async () => body };
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("what is installed against what is on GitHub", () => {
  it("shows the commit this checkout is on", async () => {
    render(<GitHubUpdateSection version="0.5" />);

    expect(await screen.findByTestId("local-commit")).toHaveTextContent("aaaaaaa");
  });

  it("shows GitHub's commit when it is a different one", async () => {
    body = BEHIND;
    render(<GitHubUpdateSection version="0.5" />);

    expect(await screen.findByTestId("remote-commit")).toHaveTextContent("bbbbbbb");
  });

  it("says it is up to date when the two match", async () => {
    render(<GitHubUpdateSection version="0.5" />);

    expect(await screen.findByTestId("update-status")).toHaveTextContent(/up to date/i);
  });

  it("says how many commits are waiting when it is behind", async () => {
    body = BEHIND;
    render(<GitHubUpdateSection version="0.5" />);

    expect(await screen.findByTestId("update-status")).toHaveTextContent("1 new commit");
  });

  it("lists what those commits are", async () => {
    body = BEHIND;
    render(<GitHubUpdateSection version="0.5" />);

    expect(await screen.findByText(/Fix the chart crash/)).toBeInTheDocument();
  });
});

describe("what it is comparing against", () => {
  it("names the repository and branch, not a hardcoded one", async () => {
    // This checkout's origin is MooreSi/forex-react and it runs
    // `react-dashboard`, while the check compares against `main`. "Up to
    // date" can therefore be true of a branch nobody is running, and the
    // screen has to be honest about which one it asked about.
    body = { ...UP_TO_DATE, tracking: { repo_url: "https://github.com/MooreSi/forex-react", branch: "main" } };
    render(<GitHubUpdateSection version="0.5" />);

    const link = await screen.findByRole("link", { name: /forex-react/ });
    expect(link).toHaveAttribute("href", "https://github.com/MooreSi/forex-react");
    expect(await screen.findByTestId("tracking-branch")).toHaveTextContent("main");
  });

  it("says nothing about a remote it could not read", async () => {
    body = { ...UP_TO_DATE, tracking: { repo_url: "", branch: "main" } };
    render(<GitHubUpdateSection version="0.5" />);
    await screen.findByTestId("local-commit");

    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("checking", () => {
  it("checks on its own when the screen opens", async () => {
    render(<GitHubUpdateSection version="0.5" />);

    await waitFor(() => expect(requested.length).toBe(1));
  });

  it("does not pay for a plain-English summary just to check", async () => {
    // The summary is a paid model call and this card lists the commit
    // subjects anyway. Only the popup that asks the operator to install
    // something is worth one.
    render(<GitHubUpdateSection version="0.5" />);

    await waitFor(() => expect(requested[0]).toContain("include_summary=false"));
  });

  it("says it is checking while the fetch is running", async () => {
    // The button did nothing visible, so a check that takes as long as a git
    // fetch takes was indistinguishable from a dead button.
    gate = () => {};
    render(<GitHubUpdateSection version="0.5" />);

    expect(await screen.findByTestId("update-status")).toHaveTextContent(/checking/i);
  });

  it("re-checks when the button is pressed", async () => {
    render(<GitHubUpdateSection version="0.5" />);
    await screen.findByTestId("local-commit");

    await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));

    await waitFor(() => expect(requested.length).toBe(2));
  });
});

describe("applying", () => {
  it("offers no update button when there is nothing to install", async () => {
    render(<GitHubUpdateSection version="0.5" />);
    await screen.findByTestId("local-commit");

    expect(screen.queryByRole("button", { name: /^update$/i })).toBeNull();
  });

  it("posts to the route that applies an update", async () => {
    // `/api/node/update` is a GET-only route. The old button posted there and
    // the operator was told nothing.
    body = BEHIND;
    render(<GitHubUpdateSection version="0.5" />);
    await userEvent.click(await screen.findByRole("button", { name: /^update$/i }));

    await waitFor(() =>
      expect(requested).toContain("POST /api/node/update/apply"));
  });
});

describe("an install that is not a git checkout", () => {
  const NOT_LINKED = {
    current: "0.5",
    update: {
      available: false, bootstrap: true,
      error: "this install could not be matched to a commit on GitHub",
    },
    changes: [], changes_error: "",
  };

  it("says it is not linked rather than calling it a failure", async () => {
    // It is the normal state of an install that has never updated — the
    // Setup scripts copy files, they never clone.
    body = NOT_LINKED;
    render(<GitHubUpdateSection version="0.5" />);

    expect(await screen.findByTestId("update-status")).toHaveTextContent(/not linked/i);
  });

  it("offers the button that links it, named for what it does", async () => {
    // It force-checks-out origin's HEAD and replaces anything changed in the
    // folder, so it is never labelled just "Update".
    body = NOT_LINKED;
    render(<GitHubUpdateSection version="0.5" />);

    expect(
      await screen.findByRole("button", { name: /update to latest/i }),
    ).toBeInTheDocument();
  });
});

describe("a check that could not run", () => {
  it("reports the reason instead of implying it is up to date", async () => {
    body = {
      current: "0.5",
      update: { available: false, error: "git fetch failed: could not resolve host" },
      changes: [], changes_error: "",
    };
    render(<GitHubUpdateSection version="0.5" />);

    expect(await screen.findByTestId("update-status")).toHaveTextContent(/check failed/i);
    expect(screen.getByText(/could not resolve host/)).toBeInTheDocument();
  });
});
