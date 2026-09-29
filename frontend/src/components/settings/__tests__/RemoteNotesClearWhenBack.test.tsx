import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RemoteRestartSection } from "../internal/RemoteRestartSection";
import { RemoteUpgradeSection } from "../internal/RemoteUpgradeSection";

/**
 * "VPS: Restarting app in 5 seconds" stayed under Restart VPS for good, long
 * after the VPS was back (owner, 2026-09-29). A note about a restart is true
 * until the VPS has gone and come back: the link dropping and returning is
 * that moment, so the note goes then.
 *
 * Nothing here restarts or updates anything: `fetch` is a recorder.
 */
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
    ok: true, status: 200,
    json: async () => (String(url).includes("/versions")
      ? { local: { commit: "a".repeat(40), git_version: "" },
          remote: { commit: "a".repeat(40), git_version: "" }, in_sync: true, last_update: null }
      : { note: "VPS: Restarting app in 5 seconds" }),
  })));
});
afterEach(() => vi.unstubAllGlobals());

const cases = [
  { name: "Restart", Section: RemoteRestartSection, open: /Restart VPS/, confirm: /^Restart the VPS/ },
  { name: "Upgrade", Section: RemoteUpgradeSection, open: /Upgrade VPS/, confirm: /^Upgrade the VPS/ },
];

describe.each(cases)("$name VPS note", ({ Section, open, confirm }) => {
  async function press() {
    const view = render(<Section connected openPositions={0} />);
    await userEvent.click(screen.getByRole("button", { name: open }));
    await userEvent.click(screen.getByRole("button", { name: confirm }));
    await screen.findByText(/Restarting app in 5 seconds/);
    return view;
  }

  it("stays while the VPS has not gone down yet", async () => {
    const { rerender } = await press();

    rerender(<Section connected openPositions={0} />);

    expect(screen.getByText(/Restarting app in 5 seconds/)).toBeTruthy();
  });

  it("stays while the VPS is down", async () => {
    const { rerender } = await press();

    rerender(<Section connected={false} openPositions={0} />);

    expect(screen.getByText(/Restarting app in 5 seconds/)).toBeTruthy();
  });

  it("goes once the VPS has dropped and come back", async () => {
    const { rerender } = await press();

    rerender(<Section connected={false} openPositions={0} />);
    rerender(<Section connected openPositions={0} />);

    await waitFor(() => expect(screen.queryByText(/Restarting app in 5 seconds/)).toBeNull());
  });

  it("can be dismissed by hand", async () => {
    await press();

    await userEvent.click(screen.getByRole("button", { name: /Dismiss this message/ }));

    expect(screen.queryByText(/Restarting app in 5 seconds/)).toBeNull();
  });
});
