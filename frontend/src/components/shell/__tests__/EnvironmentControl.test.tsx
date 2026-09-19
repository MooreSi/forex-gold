import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EnvironmentControl } from "../EnvironmentControl";

/**
 * Demo or live: which account the whole app is pointed at.
 *
 * Every other control in this dashboard decides what happens on whichever
 * account is selected. This one decides whether that account holds real money,
 * so the questions here are: can it be triggered by accident, does it name the
 * account before it acts, and does it say what it is about to do to the app.
 *
 * Nothing here switches anything: `fetch` is a recorder.
 *
 * Changed 2026-09-19: the control used to be a ghost button beside the green
 * account badge, both saying "DEMO". They are one control now -- the badge IS
 * the switch -- so these tests pass the bridge's account in, because the badge
 * shows ground truth rather than what the config file says.
 */
const DEMO_ACCOUNT = { is_demo: true, login: 5203117 };
const LIVE_ACCOUNT = { is_demo: false, login: 900123 };
let state: Record<string, unknown>;
let response: { status: number; body: unknown };
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  state = {
    current: "demo",
    environments: {
      demo: { login: "5203117", server: "Vantage-Demo", configured: true },
      live: { login: "900123", server: "Vantage-Live", configured: true },
    },
  };
  response = { status: 200, body: { environment: "live", note: "pointed at it",
                                    restart: "restarting" } };
  fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") {
      return {
        ok: response.status < 400, status: response.status,
        json: async () => response.body,
      };
    }
    return { ok: true, status: 200, json: async () => state };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const writes = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "PUT");

describe("what it shows", () => {
  it("says DEMO on a demo install", async () => {
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);

    expect(await screen.findByTestId("environment-control")).toHaveTextContent("DEMO");
  });

  it("says LIVE, loudly, on a live one", async () => {
    state.current = "live";
    render(<EnvironmentControl account={LIVE_ACCOUNT} />);

    const button = await screen.findByTestId("environment-control");
    expect(button).toHaveTextContent("LIVE");
    expect(button.querySelector(".text-loss")).not.toBeNull();
  });

  it("renders nothing at all when it cannot read its own state", async () => {
    // "DEMO" on a live account is the one outcome worth avoiding at any cost,
    // so a control that does not know says nothing rather than guessing.
    fetchMock.mockImplementation(async () => ({
      ok: false, status: 500, json: async () => ({}),
    }));
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);

    await waitFor(() =>
      expect(screen.queryByTestId("environment-control")).not.toBeInTheDocument());
  });

  it("is disabled, with a reason, when the other account is not configured", async () => {
    state.environments = {
      demo: { login: "5203117", server: "Vantage-Demo", configured: true },
      live: { login: "", server: "", configured: false },
    };
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);

    const button = await screen.findByTestId("environment-control");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("Settings > MT5"));
  });
});

describe("switching to live", () => {
  it("does not switch on the first press", async () => {
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);

    await userEvent.click(await screen.findByTestId("environment-control"));

    expect(writes()).toHaveLength(0);
  });

  it("names the account before it acts", async () => {
    // "Are you sure?" is a question people learn to click through.
    // "Switch to 900123 on Vantage-Live?" is one they read.
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);
    await userEvent.click(await screen.findByTestId("environment-control"));

    expect(screen.getByText("900123")).toBeInTheDocument();
    expect(screen.getByText("Vantage-Live")).toBeInTheDocument();
  });

  it("says it is real money", async () => {
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);
    await userEvent.click(await screen.findByTestId("environment-control"));

    expect(screen.getByText(/real money/)).toBeInTheDocument();
  });

  it("warns that the app restarts", async () => {
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);
    await userEvent.click(await screen.findByTestId("environment-control"));

    expect(screen.getByText(/app restarts to apply it/)).toBeInTheDocument();
  });

  it("cancelling sends nothing", async () => {
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);
    await userEvent.click(await screen.findByTestId("environment-control"));

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(writes()).toHaveLength(0);
  });

  it("confirming sends the target and the confirmation together", async () => {
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);
    await userEvent.click(await screen.findByTestId("environment-control"));

    await userEvent.click(screen.getByRole("button", { name: "Switch to LIVE" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      environment: "live", confirm: true,
    });
  });
});

describe("switching back to demo", () => {
  it("still asks, but does not talk about real money", async () => {
    // The safe direction. Dressing it up the same way would train the operator
    // to click through the question that matters.
    state.current = "live";
    render(<EnvironmentControl account={LIVE_ACCOUNT} />);
    await userEvent.click(await screen.findByTestId("environment-control"));

    expect(screen.getByText(/back to the demo account/)).toBeInTheDocument();
    expect(screen.queryByText(/real money/)).not.toBeInTheDocument();
  });

  it("sends demo as the target", async () => {
    state.current = "live";
    render(<EnvironmentControl account={LIVE_ACCOUNT} />);
    await userEvent.click(await screen.findByTestId("environment-control"));

    await userEvent.click(screen.getByRole("button", { name: "Switch to demo" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      environment: "demo", confirm: true,
    });
  });
});

describe("what it reports", () => {
  it("shows the backend's note and what happened to the restart", async () => {
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);
    await userEvent.click(await screen.findByTestId("environment-control"));
    await userEvent.click(screen.getByRole("button", { name: "Switch to LIVE" }));

    expect(await screen.findByRole("status")).toHaveTextContent("pointed at it");
    expect(screen.getByRole("status")).toHaveTextContent("restarting");
  });

  it("shows a refusal in the backend's own words", async () => {
    response = {
      status: 400,
      body: {
        error: {
          kind: "refusal",
          message: "No Live MT5 credentials are saved. Enter them under Settings > MT5.",
          ref: null,
        },
      },
    };
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);
    await userEvent.click(await screen.findByTestId("environment-control"));
    await userEvent.click(screen.getByRole("button", { name: "Switch to LIVE" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Settings > MT5");
  });
});

describe("when the credentials are saved but cannot be read", () => {
  it("does not tell the operator to enter credentials that are already there", async () => {
    // Seen live on 2026-09-19: both accounts fully configured, the app
    // trading on demo, and the switch disabled with "Add it under
    // Settings > MT5". The app was running under an interpreter with no
    // `keyring`, so every password decrypted to "". Re-typing them would not
    // have fixed anything.
    state.environments = {
      demo: { login: "26004592", server: "VantageMarkets-Demo",
              configured: false, unreadable: true },
      live: { login: "29377272", server: "VantageMarkets-Live 6",
              configured: false, unreadable: true },
    };
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);

    const button = await screen.findByTestId("environment-control");
    expect(button).toBeDisabled();
    expect(button.getAttribute("title")).not.toMatch(/Add it under Settings/);
  });

  it("names the account and says what is actually wrong", async () => {
    state.environments = {
      demo: { login: "26004592", server: "VantageMarkets-Demo",
              configured: true, unreadable: false },
      live: { login: "29377272", server: "VantageMarkets-Live 6",
              configured: false, unreadable: true },
    };
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);

    const title = (await screen.findByTestId("environment-control")).getAttribute("title");
    expect(title).toContain("29377272");
    expect(title).toMatch(/cannot be decrypted/);
  });

  it("still says to add them when nothing is saved at all", async () => {
    // The case where typing them IS the fix.
    state.environments = {
      demo: { login: "26004592", server: "VantageMarkets-Demo",
              configured: true, unreadable: false },
      live: { login: "", server: "", configured: false, unreadable: false },
    };
    render(<EnvironmentControl account={DEMO_ACCOUNT} />);

    expect((await screen.findByTestId("environment-control")).getAttribute("title"))
      .toMatch(/Add it under Settings > MT5/);
  });
});
