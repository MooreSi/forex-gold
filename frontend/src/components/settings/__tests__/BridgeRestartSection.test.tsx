/**
 * Settings > MT5 > Restart bridge (owner, 2026-09-29).
 *
 * On a Mac the restart takes MetaTrader and the EA down with the bridge, so
 * one press only opens a confirmation that says so; only the confirm button
 * sends the request. The result is shown in the backend's own words.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BridgeRestartSection } from "../internal/BridgeRestartSection";

let fetchMock: ReturnType<typeof vi.fn>;
const posts = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "POST");

function reply(body: unknown) {
  fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }));
  vi.stubGlobal("fetch", fetchMock);
}

beforeEach(() => reply({ ok: true, message: "Bridge restarted and connected to MT5." }));
afterEach(() => vi.unstubAllGlobals());

describe("restarting the bridge", () => {
  it("does nothing on the first press but ask", async () => {
    render(<BridgeRestartSection platform="darwin" />);
    await userEvent.click(screen.getByRole("button", { name: /restart bridge/i }));

    expect(posts()).toHaveLength(0);
    expect(screen.getByText(/MetaTrader and the EA inside it close and reopen/i))
      .toBeInTheDocument();
  });

  it("restarts on confirm and shows what happened", async () => {
    render(<BridgeRestartSection platform="darwin" />);
    await userEvent.click(screen.getByRole("button", { name: /restart bridge/i }));
    await userEvent.click(screen.getByRole("button", { name: /restart the bridge/i }));

    expect(posts()).toHaveLength(1);
    expect(String(posts()[0][0])).toBe("/api/settings/mt5/restart-bridge");
    expect(await screen.findByRole("status"))
      .toHaveTextContent("Bridge restarted and connected to MT5.");
  });

  it("shows a failed restart as a failure", async () => {
    reply({ ok: false, message: "The bridge could not be started." });
    render(<BridgeRestartSection platform="darwin" />);
    await userEvent.click(screen.getByRole("button", { name: /restart bridge/i }));
    await userEvent.click(screen.getByRole("button", { name: /restart the bridge/i }));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("The bridge could not be started.");
  });

  it("on Windows says it reconnects rather than relaunching MetaTrader", async () => {
    render(<BridgeRestartSection platform="win32" />);
    await userEvent.click(screen.getByRole("button", { name: /restart bridge/i }));

    expect(screen.getByText(/reconnects to MetaTrader/i)).toBeInTheDocument();
  });
});
