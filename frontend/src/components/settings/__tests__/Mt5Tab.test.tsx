/**
 * The MT5 fields the NiceGUI page had and the React port did not (2026-09-23).
 *
 * * **Terminal path, per account.** `mt5_bridge.py` hands it to
 *   `mt5.initialize(path=...)` when no terminal is running -- the state of a
 *   headless VPS after a reboot. Without it on screen, pointing a new VPS at
 *   its terminal meant editing the database by hand.
 * * **The Wine prefix and binary.** The Backend selector offers "Wine
 *   (independent prefix)" and says to use "the bottle path below" -- and there
 *   was no bottle path below.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Mt5Tab } from "../tabs/Mt5Tab";

const BODIES: Record<string, unknown> = {
  "/api/settings/mt5": {
    // The machine these tests describe is a Mac: the bridge section is
    // macOS-only (2026-09-26).
    platform: "darwin",
    login: "5203117", server: "Vantage-Demo", password_enc_set: true,
    terminal_path: "C:\\MT5\\terminal64.exe",
    live_login: "", live_server: "", live_password_enc_set: false,
    live_terminal_path: null,
  },
  "/api/settings/risk": { ea_bridge_enabled: 0 },
  "/api/settings/app": {
    bridge_backend: "wine", mt5_bridge_url: "http://localhost:9010",
    mt5_bottle_path: "~/.wine_mt5", wine_bin: "",
  },
};

let fetchMock: ReturnType<typeof vi.fn>;
const writes = () => fetchMock.mock.calls.filter((c) => c[1]?.method && c[1].method !== "GET");

beforeEach(() => {
  fetchMock = vi.fn(async (url: string) => ({
    ok: true, status: 200, json: async () => BODIES[String(url)] ?? {},
  }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("the terminal path", () => {
  it("shows what is stored for each account", async () => {
    render(<Mt5Tab />);

    expect(await screen.findByLabelText("Demo account terminal path"))
      .toHaveValue("C:\\MT5\\terminal64.exe");
    expect(screen.getByLabelText("Live account terminal path")).toHaveValue("");
  });

  it("saves on its own, without asking for the password again", async () => {
    render(<Mt5Tab />);

    const field = await screen.findByLabelText("Live account terminal path");
    await userEvent.type(field, "D:\\Live\\terminal64.exe");
    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/settings/mt5/terminal-path");
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      path: "D:\\Live\\terminal64.exe", environment: "live",
    });
  });
});

describe("the Wine prefix and binary", () => {
  it("shows both, next to the backend that uses them", async () => {
    render(<Mt5Tab />);

    expect(await screen.findByLabelText("Wine prefix")).toHaveValue("~/.wine_mt5");
    expect(screen.getByLabelText("Wine binary")).toHaveValue("");
  });

  it("saves the prefix to the app config", async () => {
    render(<Mt5Tab />);

    const field = await screen.findByLabelText("Wine prefix");
    await userEvent.clear(field);
    await userEvent.type(field, "~/.mt5_prefix");
    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/settings/app");
    expect(JSON.parse(writes()[0][1].body)).toEqual({ mt5_bottle_path: "~/.mt5_prefix" });
  });
});

describe("the macOS bridge section (2026-09-26)", () => {
  // "How the bridge runs" chooses between CrossOver and Wine, which exist only
  // on a Mac. On Windows the bridge runs inside the app, and the section only
  // offered settings that do nothing there.
  const original = BODIES["/api/settings/mt5"];
  afterEach(() => { BODIES["/api/settings/mt5"] = original; });

  const renderOn = async (platform: string) => {
    BODIES["/api/settings/mt5"] = { ...(original as object), platform };
    render(<Mt5Tab />);
    await screen.findAllByText(/terminal path/i);
  };

  it("is not shown on Windows", async () => {
    await renderOn("win32");

    expect(screen.queryByText("How the bridge runs")).toBeNull();
    expect(screen.queryByLabelText("Backend")).toBeNull();
  });

  it("is shown on a Mac", async () => {
    await renderOn("darwin");

    expect(await screen.findByText("How the bridge runs")).toBeInTheDocument();
  });
});
