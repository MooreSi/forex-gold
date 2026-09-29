import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VersionLine, type VersionReport } from "../internal/RemoteUpgradeSection";

/**
 * Say why the nodes differ (owner, 2026-09-29).
 *
 * Upgrade VPS pulls origin/main. The Mac ran two commits it had not pushed,
 * the VPS landed on origin/main, and the line said "Not in sync" -- which read
 * as "the upgrade did not take". It had. When the VPS is on what GitHub has
 * and this machine is not, the fix is to push, and the line says so.
 */
const MAC = "c575dd0" + "0".repeat(33);
const ORIGIN = "a8778c2" + "0".repeat(33);
const OLDER = "37c4166" + "0".repeat(33);

function report(over: Partial<VersionReport>): VersionReport {
  return {
    local: { commit: MAC, git_version: "2.50.1" },
    remote: { commit: ORIGIN, git_version: "2.55.0" },
    in_sync: false, last_update: null, remote_reason: "reported", ...over,
  };
}

const line = () => screen.getByTestId("remote-sync-state").textContent ?? "";

describe("VersionLine: which side is behind", () => {
  it("says push first when the VPS already runs what GitHub has", () => {
    render(<VersionLine report={report({ origin_commit: ORIGIN })} />);

    expect(line()).toContain("latest commit on GitHub");
    expect(line()).toContain("not pushed");
  });

  it("points at Upgrade VPS when the VPS is behind GitHub", () => {
    render(<VersionLine report={report({ remote: { commit: OLDER, git_version: "" }, origin_commit: ORIGIN })} />);

    expect(line()).toContain("Upgrade VPS");
    expect(line()).not.toContain("not pushed");
  });

  it("says the VPS has pulled but not restarted", () => {
    render(<VersionLine report={report({ restart_pending: true, origin_commit: ORIGIN })} />);

    expect(line()).toContain("not restarted");
  });

  it("keeps the plain wording from a backend with no origin commit", () => {
    render(<VersionLine report={report({})} />);

    expect(line()).toContain("Not in sync: the nodes run different commits.");
  });
});
