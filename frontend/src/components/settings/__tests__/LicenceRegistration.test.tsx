/**
 * The licence registration card.
 *
 * `/api/node/state.registration` returns `{connected, last_error, version,
 * latest_version, update_available, changelog, connected_host,
 * subscription_type, subscription_expiry, email, nickname, is_remote_admin,
 * registration_sent_at}`.
 *
 * The card read `registration["approved"]`, which is not one of them, so every
 * install — registered or not — was told "This machine has not been approved
 * yet". Verified against the running app on 2026-09-20.
 *
 * The NiceGUI page this replaced keyed on `connected` and showed the
 * subscription, its expiry with a day count, and the registered email. Those
 * were never ported.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NodeTab } from "../tabs/NodeTab";

const BASE = {
  version: "0.5", active_trader: "local", sync_token_set: false,
  registered_email: "simon@example.com",
  autostart: { supported: true, installed: false, armed: false },
  registration: {
    connected: false, last_error: "", version: "0.5", latest_version: "",
    update_available: false, changelog: [], connected_host: "",
    subscription_type: "", subscription_expiry: "", email: "", nickname: "",
    is_remote_admin: false, registration_sent_at: 0,
  },
};

function serve(registration: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const body = String(url).includes("/api/node/state")
      ? { ...BASE, registration }
      : String(url).includes("/api/node/update")
        ? { current: "0.5", update: { available: false, commits: [] }, changes: [] }
        : {};
    return { ok: true, status: 200, json: async () => body };
  }));
}

beforeEach(() => serve(BASE.registration));
afterEach(() => vi.unstubAllGlobals());

describe("an unregistered machine", () => {
  it("says it is not registered yet", async () => {
    render(<NodeTab />);

    expect(await screen.findByText(/not been approved yet/)).toBeInTheDocument();
  });

  it("shows the last error when there is one", async () => {
    // Otherwise "not approved" is all the operator gets, and they cannot
    // tell a refusal from a machine that never reached the server.
    serve({ ...BASE.registration, last_error: "licence server unreachable" });
    render(<NodeTab />);

    expect(await screen.findByText(/licence server unreachable/)).toBeInTheDocument();
  });
});

describe("a registered machine", () => {
  const CONNECTED = {
    ...BASE.registration,
    connected: true, subscription_type: "Annual",
    subscription_expiry: "2027-01-15", email: "simon@example.com",
  };

  it("says it is authorised, which it never did before", async () => {
    serve(CONNECTED);
    render(<NodeTab />);

    expect(await screen.findByText(/Authorised and registered/)).toBeInTheDocument();
  });

  it("names the subscription", async () => {
    serve(CONNECTED);
    render(<NodeTab />);

    expect(await screen.findByTestId("licence-subscription"))
      .toHaveTextContent("Annual");
  });

  it("shows the expiry", async () => {
    serve(CONNECTED);
    render(<NodeTab />);

    expect(await screen.findByTestId("licence-expiry"))
      .toHaveTextContent("2027-01-15");
  });

  it("calls a perpetual licence what it is, rather than showing a blank", async () => {
    serve({ ...CONNECTED, subscription_type: "", subscription_expiry: "" });
    render(<NodeTab />);

    expect(await screen.findByTestId("licence-subscription"))
      .toHaveTextContent("Perpetual");
    expect(screen.getByTestId("licence-expiry")).toHaveTextContent("Never");
  });

  it("shows the registered email", async () => {
    serve(CONNECTED);
    render(<NodeTab />);

    expect(await screen.findByTestId("licence-email"))
      .toHaveTextContent("simon@example.com");
  });
});
