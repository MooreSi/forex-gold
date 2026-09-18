import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "../AppShell";
import { DEFAULT_TAB, TABS } from "../tabs";
import { AuthProvider } from "@/contexts/AuthContext";
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
  it("has the ten tabs, with the names and in the order the NiceGUI shell used", () => {
    // frontend/app/__init__.py:411-420. The order is not cosmetic: it is the
    // order the operator has learned.
    expect(TABS.map((t) => t.label)).toEqual([
      "AI Analysis", "Chart", "Trading", "Parsing", "Signal Generator",
      "Backtest", "Analysis", "Settings", "News", "About",
    ]);
  });

  it("renders every one of them", () => {
    renderShell();
    const tabs = screen.getAllByRole("tab").filter((t) =>
      TABS.some((s) => t.textContent?.startsWith(s.label)));
    expect(tabs).toHaveLength(10);
  });

  it("opens on Chart, as the app always has", () => {
    expect(DEFAULT_TAB).toBe("chart");
    renderShell();
    expect(screen.getByRole("tab", { name: /^Chart/ })).toHaveAttribute("data-state", "active");
  });
});

describe("tabs the port has not reached", () => {
  it("says so, and names the task that will fill it", async () => {
    // Hiding the tab would read as a lost feature. See QUESTIONS.md Q1.
    renderShell();
    await userEvent.click(screen.getByRole("tab", { name: /^Settings/ }));
    expect(screen.getByText(/has not been rebuilt in React yet/)).toBeInTheDocument();
    expect(screen.getByText("080-remaining-tabs")).toBeInTheDocument();
    expect(screen.getByText("frontend/pages/settings/")).toBeInTheDocument();
  });

  it("marks the unported tabs in the strip so the gap is visible before clicking", () => {
    renderShell();
    const wip = screen.getAllByText("wip");
    expect(wip).toHaveLength(TABS.filter((t) => t.notPorted).length);
  });

  it("does NOT mark the two that are ported", () => {
    // Negative control: a placeholder marker on every tab would make the test
    // above pass while telling the operator nothing.
    const ported = TABS.filter((t) => t.notPorted === null).map((t) => t.id);
    expect(ported).toEqual(["chart", "trading"]);
  });
});
