import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RemoteUpgradeSection, VersionLine, type VersionReport } from "../internal/RemoteUpgradeSection";

/**
 * Upgrade the VPS from here (owner, 2026-09-27), and show whether both nodes
 * run the same commit. It closes nothing but manages nothing while the VPS
 * restarts, so it never happens on one press.
 *
 * Nothing here updates anything: `fetch` is a recorder.
 */
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (url: string) => ({
    ok: true, status: 200,
    json: async () => (String(url).includes("/versions")
      ? { local: { commit: "a".repeat(40), git_version: "2.39.5" },
          remote: { commit: "a".repeat(40), git_version: "2.45.1" }, in_sync: true, last_update: null }
      : { note: "VPS: updating" }),
  }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const posts = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "POST");

describe("RemoteUpgradeSection", () => {
  it("sends nothing on the first press", async () => {
    render(<RemoteUpgradeSection connected openPositions={0} />);
    await userEvent.click(screen.getByRole("button", { name: /Upgrade VPS/ }));
    expect(posts()).toHaveLength(0);
  });

  it("upgrades through the remote endpoint once confirmed", async () => {
    render(<RemoteUpgradeSection connected openPositions={2} />);
    await userEvent.click(screen.getByRole("button", { name: /Upgrade VPS/ }));
    expect(screen.getByText(/2 open positions keep their SL\/TP/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Upgrade the VPS/ }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(String(posts()[0][0])).toContain("/api/remote/update-vps");
  });

  it("cannot be pressed without a link", () => {
    render(<RemoteUpgradeSection connected={false} openPositions={0} />);
    expect(screen.getByRole("button", { name: /Upgrade VPS/ })).toBeDisabled();
  });
});

function report(over: Partial<VersionReport> = {}): VersionReport {
  return {
    local: { commit: "abc1234" + "0".repeat(33), git_version: "2.39.5" },
    remote: { commit: "abc1234" + "0".repeat(33), git_version: "2.45.1" },
    in_sync: true, last_update: null, ...over,
  };
}

describe("VersionLine", () => {
  it("shows both commits and git versions, and in sync", () => {
    render(<VersionLine report={report()} />);
    expect(screen.getByTestId("remote-versions"))
      .toHaveTextContent("This machine abc1234 · git 2.39.5 · VPS abc1234 · git 2.45.1");
    expect(screen.getByTestId("remote-sync-state")).toHaveTextContent("In sync");
  });

  it("says when they differ", () => {
    render(<VersionLine report={report({ remote: { commit: "def5678" + "0".repeat(33), git_version: "" }, in_sync: false })} />);
    expect(screen.getByTestId("remote-sync-state")).toHaveTextContent("Not in sync");
  });

  it("an older VPS is unknown, not out of sync", () => {
    render(<VersionLine report={report({ remote: null, in_sync: null })} />);
    expect(screen.getByTestId("remote-sync-state")).toHaveTextContent("has not reported its commit");
  });

  it("shows a failed update the VPS reported", () => {
    render(<VersionLine report={report({ last_update: { ok: false, note: "git fetch failed" } })} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Last VPS update failed: git fetch failed");
  });
});

describe("VersionLine robustness", () => {
  it("shows nothing for a reply that is not a version report", () => {
    const { container } = render(<VersionLine report={{ conn_state: "connected" } as unknown as VersionReport} />);
    expect(container).toBeEmptyDOMElement();
  });
});
