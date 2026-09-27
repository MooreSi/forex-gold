import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RemoteRestartSection } from "../internal/RemoteRestartSection";

/**
 * Restart the VPS from here, so nobody has to log in to it (owner,
 * 2026-09-26).
 *
 * Like the header's own Restart, it closes nothing: positions keep their SL/TP
 * at the broker, but nothing on the VPS manages them while it is down. So it
 * never happens on one press, and the confirmation says how many are open.
 *
 * Nothing here restarts anything: `fetch` is a recorder.
 */
let fetchMock: ReturnType<typeof vi.fn>;
let reply: { ok: boolean; status: number; body: unknown };

beforeEach(() => {
  reply = { ok: true, status: 200, body: { note: "Restarting app in 5 seconds" } };
  fetchMock = vi.fn(async () => ({
    ok: reply.ok, status: reply.status, json: async () => reply.body,
  }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const posts = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "POST");

describe("RemoteRestartSection", () => {
  it("sends nothing on the first press", async () => {
    render(<RemoteRestartSection connected openPositions={0} />);

    await userEvent.click(screen.getByRole("button", { name: /Restart VPS/ }));

    expect(posts()).toHaveLength(0);
  });

  it("restarts through the remote endpoint once confirmed", async () => {
    render(<RemoteRestartSection connected openPositions={0} />);
    await userEvent.click(screen.getByRole("button", { name: /Restart VPS/ }));

    await userEvent.click(screen.getByRole("button", { name: /^Restart the VPS/ }));

    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0][0]).toBe("/api/remote/restart-vps");
    expect(await screen.findByText(/Restarting app in 5 seconds/)).toBeTruthy();
  });

  it("names the open positions it will leave unmanaged for a while", async () => {
    render(<RemoteRestartSection connected openPositions={2} />);

    await userEvent.click(screen.getByRole("button", { name: /Restart VPS/ }));

    expect(screen.getByText(/2 open positions/)).toBeTruthy();
  });

  it("cannot be pressed without a link", () => {
    render(<RemoteRestartSection connected={false} openPositions={0} />);

    expect(
      (screen.getByRole("button", { name: /Restart VPS/ }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("shows the reason when the VPS says no", async () => {
    reply = {
      ok: false, status: 409,
      body: { error: { kind: "refusal", message: "The VPS did not answer." } },
    };
    render(<RemoteRestartSection connected openPositions={0} />);
    await userEvent.click(screen.getByRole("button", { name: /Restart VPS/ }));

    await userEvent.click(screen.getByRole("button", { name: /^Restart the VPS/ }));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("The VPS did not answer.");
  });
});
