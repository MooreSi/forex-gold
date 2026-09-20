/**
 * The Diagnostics circuit-breaker card.
 *
 * `/api/settings/diagnostics` returns the breaker as
 * `{enabled, losses_threshold, cooldown_mins, active_until, consec_losses,
 *   is_active, remaining_secs}`. The card read `breaker["tripped"]`, a key the
 * endpoint has never returned, so:
 *
 *   * it said "Circuit breaker clear" whatever the breaker was doing — on the
 *     one screen whose job is to say whether orders are being blocked;
 *   * its Reset button was permanently disabled, with the reason "The breaker
 *     is not tripped", so a breaker that HAD tripped could not be cleared here
 *     at all.
 *
 * The fixture in SettingsPanel.test.tsx invented `{tripped, reason}` to match,
 * which is why the existing assertions were green.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DiagnosticsTab } from "../tabs/DiagnosticsTab";

const CLEAR = {
  enabled: true, losses_threshold: 3, cooldown_mins: 15,
  active_until: 0, consec_losses: 0, is_active: false, remaining_secs: 0,
};

const TRIPPED = {
  enabled: true, losses_threshold: 3, cooldown_mins: 60,
  active_until: 1_789_900_000, consec_losses: 3, is_active: true,
  remaining_secs: 1_500,
};

let posts: string[] = [];

function serve(breaker: Record<string, unknown>) {
  posts = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") posts.push(String(url));
    return {
      ok: true, status: 200,
      json: async () => ({ log: [], circuit_breaker: breaker }),
    };
  }));
}

beforeEach(() => serve(CLEAR));
afterEach(() => vi.unstubAllGlobals());

describe("a clear breaker", () => {
  it("says so", async () => {
    render(<DiagnosticsTab />);

    const card = await screen.findByTestId("circuit-breaker");
    expect(card).toHaveAttribute("data-tripped", "false");
    expect(card).toHaveTextContent("Circuit breaker clear");
  });

  it("offers no Reset, and says why", async () => {
    render(<DiagnosticsTab />);

    const button = await screen.findByRole("button", { name: "Reset" });
    expect(button).toBeDisabled();
  });
});

describe("a tripped breaker", () => {
  beforeEach(() => serve(TRIPPED));

  it("says it is tripped", async () => {
    render(<DiagnosticsTab />);

    const card = await screen.findByTestId("circuit-breaker");
    expect(card).toHaveAttribute("data-tripped", "true");
    expect(card).toHaveTextContent("Circuit breaker tripped");
  });

  it("says what tripped it", async () => {
    render(<DiagnosticsTab />);

    expect(await screen.findByTestId("circuit-breaker"))
      .toHaveTextContent("3 consecutive losses");
  });

  it("says how long is left, because it clears itself", async () => {
    render(<DiagnosticsTab />);

    expect(await screen.findByTestId("circuit-breaker"))
      .toHaveTextContent("25m");
  });

  it("can actually be reset", async () => {
    // The button was disabled in exactly the state it exists for.
    render(<DiagnosticsTab />);
    const button = await screen.findByRole("button", { name: "Reset" });

    expect(button).toBeEnabled();
    await userEvent.click(button);

    expect(posts.some((u) => u.includes("/api/settings/circuit-breaker/reset")))
      .toBe(true);
  });
});

describe("a breaker that is switched off", () => {
  it("says so rather than implying it is standing guard", async () => {
    serve({ ...CLEAR, enabled: false });
    render(<DiagnosticsTab />);

    expect(await screen.findByTestId("circuit-breaker"))
      .toHaveTextContent("Circuit breaker off");
  });
});

describe("exporting the logs", () => {
  /**
   * The NiceGUI app had an Export Logs button; the React port had nothing, so
   * the only way to get logs off a machine was to find them on disk.
   */
  function serveWithBundle(ok: boolean) {
    posts = [];
    const fetchMock = vi.fn(async (url: string) => {
      if (String(url).includes("/log-bundle")) {
        return {
          ok, status: ok ? 200 : 500,
          blob: async () => new Blob(["log text"]),
          headers: new Headers({
            "content-disposition": 'attachment; filename="forex_trader_logs_x.txt"',
            "x-log-lines-kept": "42",
            "x-log-lines-scanned": "12000",
            "x-log-truncated": "0",
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ log: [], circuit_breaker: CLEAR }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      createObjectURL: () => "blob:x", revokeObjectURL: () => {},
    });
    return fetchMock;
  }

  it("offers the button", async () => {
    serveWithBundle(true);
    render(<DiagnosticsTab />);

    expect(await screen.findByRole("button", { name: /Export logs/ }))
      .toBeInTheDocument();
  });

  it("says how much was kept, so a quiet bundle does not read as a failure", async () => {
    serveWithBundle(true);
    render(<DiagnosticsTab />);

    await userEvent.click(await screen.findByRole("button", { name: /Export logs/ }));

    const note = await screen.findByTestId("log-export-note");
    expect(note).toHaveTextContent("42 lines kept of 12000 scanned");
  });

  it("says so when the export fails, rather than appearing to do nothing", async () => {
    serveWithBundle(false);
    render(<DiagnosticsTab />);

    await userEvent.click(await screen.findByRole("button", { name: /Export logs/ }));

    expect(await screen.findByTestId("log-export-note"))
      .toHaveTextContent(/Could not export the logs/);
  });
});
