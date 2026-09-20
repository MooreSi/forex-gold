/**
 * The EA badge, and the popup behind it when the build is stale.
 *
 * The badge has said "EA STALE BUILD" since 2026-09-09 and stopped there: an
 * operator who saw it had to go and find `tools/deploy_ea.sh`, run it in a
 * terminal, then compile. Asked for on 2026-09-20 -- click it, see what is
 * wrong, press one button.
 *
 * The rule the popup must not break: it never claims a compile it did not
 * perform. MetaEditor exits 0 on a build it never ran, and a green "done"
 * over an unchanged .ex5 is precisely the silent staleness this area exists
 * to end.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EaBadge } from "../EaBadge";

const HEALTHY = {
  colour: "green", text: "EA", tooltip: "EA connected on this node",
  stale: false, scope: "this node",
};
const STALE = {
  colour: "orange", text: "EA STALE BUILD",
  tooltip: "The EA on the chart is v1.08, but its source has been edited since "
    + "it was compiled (99 hours of changes it does not have).",
  stale: true, scope: "this node",
};

let statusBody: Record<string, unknown>;
let installBody: Record<string, unknown>;
let installStatus: number;
let posts: string[];

beforeEach(() => {
  posts = [];
  installStatus = 200;
  statusBody = {
    stale: true, detail: STALE.tooltip, binary_shipped: false, platform: "darwin",
  };
  installBody = {
    report: { targets: ["/t1"], deployed: 1, already_current: 0,
              needs_compile: 1, errors: [] },
    needs_compile: true, compiled: false,
    next_step: "Open MetaEditor, open ForexTraderBridge.mq5 and press F7 to "
      + "compile it. The chart reloads the new build by itself afterwards.",
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push(String(url));
      return {
        ok: installStatus < 400, status: installStatus,
        json: async () => (installStatus < 400
          ? installBody
          : { error: { kind: "refusal", message: "No MetaTrader terminal was found" } }),
      };
    }
    return { ok: true, status: 200, json: async () => statusBody };
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("the badge", () => {
  it("is not clickable when the build is current", () => {
    // Nothing to fix, so nothing to open. A button that opens an empty
    // dialog teaches people the badge is noise.
    render(<EaBadge badge={HEALTHY} />);

    expect(screen.queryByRole("button")).toBeNull();
  });

  it("still shows the healthy badge", () => {
    render(<EaBadge badge={HEALTHY} />);

    expect(screen.getByText("EA")).toBeInTheDocument();
  });

  it("becomes a button when the build is stale", () => {
    render(<EaBadge badge={STALE} />);

    expect(screen.getByRole("button", { name: /EA STALE BUILD/ })).toBeInTheDocument();
  });

  it("renders nothing at all when the backend sent no badge", () => {
    render(<EaBadge badge={null} />);

    expect(screen.queryByText(/EA/)).toBeNull();
  });
});

describe("the popup", () => {
  it("explains what is stale, in the backend's words", async () => {
    render(<EaBadge badge={STALE} />);
    await userEvent.click(screen.getByRole("button", { name: /EA STALE BUILD/ }));

    expect(await screen.findByText(/99 hours of changes/)).toBeInTheDocument();
  });

  it("warns that a compile is still needed when no binary is shipped", async () => {
    render(<EaBadge badge={STALE} />);
    await userEvent.click(screen.getByRole("button", { name: /EA STALE BUILD/ }));

    expect(await screen.findByText(/MetaEditor/)).toBeInTheDocument();
  });

  it("promises a one-click install when a compiled build is shipped", async () => {
    statusBody = { ...statusBody, binary_shipped: true };
    render(<EaBadge badge={STALE} />);
    await userEvent.click(screen.getByRole("button", { name: /EA STALE BUILD/ }));

    expect(await screen.findByText(/no MetaEditor/i)).toBeInTheDocument();
  });

  it("installs through the endpoint that installs", async () => {
    render(<EaBadge badge={STALE} />);
    await userEvent.click(screen.getByRole("button", { name: /EA STALE BUILD/ }));
    await userEvent.click(await screen.findByRole("button", { name: /install/i }));

    await waitFor(() => expect(posts).toEqual(["/api/settings/ea/install"]));
  });

  it("shows the F7 instruction rather than claiming it is done", async () => {
    // The rule. A compile that did not happen must never read as success.
    render(<EaBadge badge={STALE} />);
    await userEvent.click(screen.getByRole("button", { name: /EA STALE BUILD/ }));
    await userEvent.click(await screen.findByRole("button", { name: /install/i }));

    expect(await screen.findByText(/press F7/)).toBeInTheDocument();
  });

  it("says it is finished when nothing more is needed", async () => {
    installBody = {
      report: { targets: ["/t1"], deployed: 1, already_current: 0,
                needs_compile: 0, errors: [] },
      needs_compile: false, compiled: false,
      next_step: "The compiled EA was installed. The chart reloads it by itself.",
    };
    render(<EaBadge badge={STALE} />);
    await userEvent.click(screen.getByRole("button", { name: /EA STALE BUILD/ }));
    await userEvent.click(await screen.findByRole("button", { name: /install/i }));

    expect(await screen.findByText(/reloads it by itself/)).toBeInTheDocument();
  });

  it("reports how many terminals it reached", async () => {
    render(<EaBadge badge={STALE} />);
    await userEvent.click(screen.getByRole("button", { name: /EA STALE BUILD/ }));
    await userEvent.click(await screen.findByRole("button", { name: /install/i }));

    expect(await screen.findByTestId("ea-install-report")).toHaveTextContent("1");
  });

  it("puts a refusal on screen in the backend's words", async () => {
    installStatus = 409;
    render(<EaBadge badge={STALE} />);
    await userEvent.click(screen.getByRole("button", { name: /EA STALE BUILD/ }));
    await userEvent.click(await screen.findByRole("button", { name: /install/i }));

    expect(await screen.findByText(/No MetaTrader terminal was found/))
      .toBeInTheDocument();
  });
});
