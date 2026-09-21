/**
 * What the app does while the backend is not there.
 *
 * Both reported on 2026-09-21, and they are the same bug seen twice:
 *
 *   * "when i restarted the app it asked for login details but auto login is
 *     set to enabled" -- the session read failed while the server was down,
 *     the catch treated that as a session, and an unreachable server became
 *     "signed out";
 *   * "when i restart the app it should reload the app upon restart instead
 *     of me having to refresh the page" -- nothing noticed the server coming
 *     back, so the page sat on stale state until a manual refresh.
 *
 * The distinction that fixes both: a server that ANSWERS 401 is an answer,
 * and a server that cannot be reached at all is not. `fetch` throws a
 * TypeError for the second and the client throws `ApiError` for the first.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";

const SESSION = {
  authenticated: false, needs_setup: false, auto_login: true, debug: false,
};

let sessionBody: Record<string, unknown>;
let mode: "ok" | "down" | "unauthorised";
let reloads: number;

beforeEach(() => {
  reloads = 0;
  mode = "ok";
  sessionBody = { ...SESSION };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (mode === "down") throw new TypeError("Failed to fetch");
    if (mode === "unauthorised" && String(url).includes("/api/auth/session")) {
      return {
        ok: false, status: 401,
        json: async () => ({ error: { kind: "auth", message: "sign in" } }),
      };
    }
    return { ok: true, status: 200, json: async () => sessionBody };
  }));
  // jsdom's location.reload cannot be assigned to directly.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload: () => { reloads += 1; } },
  });
});
afterEach(() => vi.unstubAllGlobals());

describe("auto login", () => {
  it("does not ask for a password when the operator enabled auto login", async () => {
    render(<App />);

    await waitFor(() =>
      expect(screen.queryByLabelText(/password/i)).toBeNull());
  });
});

describe("while the backend is restarting", () => {
  it("does not show the login form just because the server is unreachable", async () => {
    // The reported bug. An unreachable server is not evidence of anything
    // about this operator's session.
    mode = "down";
    render(<App />);

    await waitFor(() => expect(screen.getByText(/reconnect/i)).toBeInTheDocument());
    expect(screen.queryByLabelText(/password/i)).toBeNull();
  });

  it("says it is reconnecting rather than sitting on 'Starting'", async () => {
    mode = "down";
    render(<App />);

    expect(await screen.findByText(/reconnect/i)).toBeInTheDocument();
  });

  it("reloads itself once the backend answers again", async () => {
    // So a restart brings the app back without the operator refreshing.
    mode = "down";
    render(<App />);
    await screen.findByText(/reconnect/i);

    mode = "ok";

    await waitFor(() => expect(reloads).toBeGreaterThan(0), { timeout: 5000 });
  });

  it("does not reload when the backend was never away", async () => {
    // A reload on every session read would be a refresh loop.
    render(<App />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    expect(reloads).toBe(0);
  });
});

describe("a server that answers", () => {
  it("treats 401 as signed out, not as a connection problem", async () => {
    // The other half of the distinction: an answer is an answer.
    mode = "unauthorised";
    render(<App />);

    await waitFor(() => expect(screen.queryByText(/reconnect/i)).toBeNull());
  });
});
