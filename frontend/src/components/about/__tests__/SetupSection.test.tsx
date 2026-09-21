import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { SetupSection } from "../internal/SetupSection";
import { COMMON_SETUP, MAC_SETUP, VPS_SETUP, WINDOWS_SETUP } from "../content/setup";

/**
 * Setup instructions.
 *
 * Owner, 2026-09-21: "about - missing the setup instructions, use what was in
 * the original forex app and also update as required due to the various app
 * updates". The NiceGUI About page had them behind a Windows / Mac / VPS tab
 * set plus a common section; the React port carried over the risk warning,
 * the glossary and the changelog and left these behind, so a fresh install
 * had nothing in the app telling it how to get MT5, the bridge or the licence
 * working.
 *
 * The test that earns its place is the LAST one: every Settings path in the
 * original names a NiceGUI page that no longer exists. Instructions that
 * confidently send someone to a screen that is not there are worse than no
 * instructions, and nothing else would catch it.
 */
describe("the platform tabs", () => {
  it("opens on Windows", async () => {
    render(<SetupSection />);

    expect(await screen.findByRole("tab", { name: "Windows" }))
      .toHaveAttribute("aria-selected", "true");
  });

  it("offers Windows, Mac and VPS", () => {
    render(<SetupSection />);

    expect(screen.getAllByRole("tab").map((t) => t.textContent))
      .toEqual(["Windows", "Mac", "VPS"]);
  });

  it("shows the chosen platform's steps and not another's", async () => {
    render(<SetupSection />);
    await userEvent.click(screen.getByRole("tab", { name: "Mac" }));

    expect(await screen.findByText(/CrossOver is needed/)).toBeInTheDocument();
    expect(screen.queryByText(/No compatibility layer is needed/)).toBeNull();
  });

  it("numbers the steps, so a half-finished setup can be resumed", async () => {
    render(<SetupSection />);

    const card = await screen.findByTestId("setup-card-0");
    expect(within(card).getByText("1.")).toBeInTheDocument();
  });
});

describe("the parts that are the same everywhere", () => {
  it("is shown under the platform tabs, not inside one of them", async () => {
    // The EA, Telegram, Resend, the API key and the licence do not depend on
    // the platform. Filed under Windows, a Mac user would never read them.
    render(<SetupSection />);
    await userEvent.click(screen.getByRole("tab", { name: "VPS" }));

    expect(await screen.findByText(/Licence key/)).toBeInTheDocument();
  });

  it("covers the EA, both Telegram halves, email, the API key and going live", () => {
    render(<SetupSection />);

    const titles = COMMON_SETUP.map((c) => c.title);
    expect(titles.join(" | ")).toMatch(/Expert Advisor/);
    expect(titles.join(" | ")).toMatch(/Telegram bot/);
    expect(titles.join(" | ")).toMatch(/Telegram reader/);
    expect(titles.join(" | ")).toMatch(/email reports/i);
    expect(titles.join(" | ")).toMatch(/Anthropic/);
    expect(titles.join(" | ")).toMatch(/Going live/);
  });
});

describe("the instructions match this build", () => {
  /**
   * The failure mode this exists for: a faithful transcription of the NiceGUI
   * page sends the operator to "Settings > Bridge & Config", "Settings >
   * Telegram Reader" and "Settings > Registration", none of which exist, and
   * quotes port 9000 for a bridge that listens on 9010 here.
   */
  const ALL = [...WINDOWS_SETUP, ...MAC_SETUP, ...VPS_SETUP, ...COMMON_SETUP]
    .flatMap((c) => c.steps).join("\n");

  const TABS = ["MT5", "Connections", "AI", "Node & updates", "Remote node",
                "Expert tunables", "Diagnostics", "Access & licence", "Appearance"];

  it("names only Settings tabs this build actually has", () => {
    // Longest known tab name first, so "Node & updates" is not matched as
    // "Node" -- and anything that matches NONE of them is the failure this
    // test is for.
    const known = [...TABS].sort((a, b) => b.length - a.length);
    const mentions = [...ALL.matchAll(/Settings\s*→\s*(.{0,20})/g)].map((m) => m[1]);

    expect(mentions.length).toBeGreaterThan(5);
    expect(mentions.filter((rest) => !known.some((t) => rest.startsWith(t))))
      .toEqual([]);
  });

  it("does not send anyone to a NiceGUI page that no longer exists", () => {
    expect(ALL).not.toMatch(/Bridge & Config/);
    expect(ALL).not.toMatch(/Settings > /);
    expect(ALL).not.toMatch(/History tab/);
  });

  it("quotes this build's ports, not the original app's", () => {
    // 9000 and 9101 are the ORIGINAL app's bridge and EA ports. Both moved so
    // that the two apps can share a machine without one trading through the
    // other's bridge -- backend/src/config/__init__.py.
    expect(ALL).toMatch(/9010/);
    expect(ALL).toMatch(/9111/);
    expect(ALL).not.toMatch(/localhost:9000/);
    expect(ALL).not.toMatch(/\b9101\b/);
  });
});
