import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VersionLine, type VersionReport } from "../internal/RemoteUpgradeSection";

/**
 * Why the VPS's commit is unknown, said plainly (owner, 2026-09-28).
 *
 * The tab said "not connected, or an older version" while the VPS was
 * connected and trading on an older build. The two need different fixes, so
 * the backend now says which (`remote_reason`) and the line follows it.
 */
const LOCAL = { commit: "e613be5034b96e4e96fefe03ec58f54d101f704c", git_version: "2.50.1" };

function report(reason: string): VersionReport {
  return {
    local: LOCAL, remote: null, in_sync: null, last_update: null, remote_reason: reason,
  } as VersionReport;
}

describe("VersionLine: why the VPS is unknown", () => {
  it("names an older build, and how to update it once", () => {
    render(<VersionLine report={report("older_build")} />);

    const line = screen.getByTestId("remote-sync-state").textContent ?? "";
    expect(line).toContain("older version");
    expect(line).toContain("admin console");
    expect(line).not.toContain("not connected");
  });

  it("names a missing link as the reason, not the version", () => {
    render(<VersionLine report={report("not_connected")} />);

    const line = screen.getByTestId("remote-sync-state").textContent ?? "";
    expect(line).toContain("Not connected");
    expect(line).not.toContain("older version");
  });
});
