import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "../AppShell";
import { DEFAULT_TAB, TABS } from "../tabs";
import { AuthProvider } from "@/contexts/AuthContext";
import { NotPortedPanel } from "@/components/shared/NotPortedPanel";
import { resetPolls } from "@/hooks/usePoll";

beforeEach(() => {
  resetPolls();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true, status: 200, json: async () => ({}),
  }));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const renderShell = () => render(<AuthProvider><AppShell /></AuthProvider>);

describe("the tab strip", () => {
  it("still carries the NiceGUI ten, with its names and in its order", () => {
    // frontend/app/__init__.py:411-420. The order is not cosmetic: it is the
    // order the operator has learned. Dashboard was added IN FRONT of it on
    // 2026-09-22 at the owner's request, which is why this asserts the tail
    // rather than the whole list -- the ten must not be reordered or renamed
    // by anything added later.
    expect(TABS.map((t) => t.label).slice(1)).toEqual([
      "AI Analysis", "Chart", "Trading", "Parsing", "Signal Generator",
      "Backtest", "Analysis", "Settings", "News", "About",
    ]);
  });

  it("leads with Dashboard, the one screen that summarises the rest", () => {
    expect(TABS[0]).toMatchObject({ id: "dashboard", label: "Dashboard" });
  });

  it("renders every one of them", () => {
    renderShell();
    const tabs = screen.getAllByRole("tab").filter((t) =>
      TABS.some((s) => t.textContent?.startsWith(s.label)));
    expect(tabs).toHaveLength(TABS.length);
    expect(TABS).toHaveLength(11);
  });

  it("opens on Dashboard", () => {
    // Chart until 2026-09-22, when the owner asked for the Dashboard to be
    // the landing screen. The tab the app opens on is rendered on every
    // start, so this pins WHICH one rather than merely that one is active:
    // a default pointing at an id with no panel renders the not-ported
    // placeholder to every user, on every launch.
    expect(DEFAULT_TAB).toBe("dashboard");
    renderShell();
    expect(screen.getByRole("tab", { name: /^Dashboard/ }))
      .toHaveAttribute("data-state", "active");
    expect(screen.getByRole("tab", { name: /^Chart/ }))
      .toHaveAttribute("data-state", "inactive");
  });

  it("opens on a tab that has a real panel behind it", () => {
    // The guard the rename above could otherwise lose: DEFAULT_TAB is a
    // string, and a typo in it is invisible until somebody launches the app.
    renderShell();
    expect(TABS.map((t) => t.id)).toContain(DEFAULT_TAB);
    expect(screen.queryByText(/has not been rebuilt in React yet/)).toBeNull();
  });
});

describe("every tab is ported", () => {
  it("has no tab left marked as work in progress", () => {
    // The port finished on 2026-09-18. This is the assertion that says so, and
    // the one that fails the day somebody adds a tab and forgets to build it.
    expect(TABS.filter((t) => t.notPorted !== null)).toEqual([]);
  });

  it("renders a real panel for every tab, not a placeholder", async () => {
    renderShell();

    for (const tab of TABS) {
      await userEvent.click(screen.getByRole("tab", { name: new RegExp(`^${tab.label}`) }));
      expect(screen.queryByText(/has not been rebuilt in React yet/)).toBeNull();
    }
  });

  it("shows no work-in-progress marker in the strip", () => {
    renderShell();

    expect(screen.queryAllByText("wip")).toHaveLength(0);
  });
});

describe("the not-ported placeholder itself", () => {
  it("still names the task and the origin, for whoever adds the next tab", () => {
    // Kept although nothing uses it today: a tab added tomorrow needs somewhere
    // honest to point at while it is being built, and deleting the component
    // would mean the next person hides the tab instead.
    render(
      <NotPortedPanel tab="Something New" task="090-something" origin="frontend/pages/x.py" />,
    );

    expect(screen.getByText(/has not been rebuilt in React yet/)).toBeInTheDocument();
    expect(screen.getByText("090-something")).toBeInTheDocument();
    expect(screen.getByText("frontend/pages/x.py")).toBeInTheDocument();
  });
});
