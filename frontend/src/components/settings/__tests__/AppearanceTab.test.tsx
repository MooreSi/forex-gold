import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppearanceTab } from "../tabs/AppearanceTab";
import { ThemeProvider } from "@/contexts/ThemeContext";

/**
 * Where the operator picks light, dark or auto.
 *
 * The only setting in this app that writes nothing to the trading database.
 * It is a display preference, and the tests say so: pressing these buttons
 * must not produce an HTTP request, because a colour is not something the
 * engines read.
 */
function fakeStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
    key: () => null,
    get length() { return map.size; },
  } as Storage;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubGlobal("localStorage", fakeStorage());
  vi.stubGlobal("matchMedia", () => ({
    matches: true, media: "", addEventListener: () => {}, removeEventListener: () => {},
  }));
  fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }));
  vi.stubGlobal("fetch", fetchMock);
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => vi.unstubAllGlobals());

const renderTab = () => render(<ThemeProvider><AppearanceTab /></ThemeProvider>);

describe("AppearanceTab", () => {
  it("offers all three choices", async () => {
    renderTab();

    for (const name of ["Auto", "Light", "Dark"]) {
      expect(screen.getByRole("radio", { name: new RegExp(name) })).toBeInTheDocument();
    }
  });

  it("marks the current choice, not just styles it", async () => {
    // A selected state that only exists as a background colour cannot be read
    // by anything but an eye.
    renderTab();

    expect(screen.getByRole("radio", { name: /Auto/ })).toBeChecked();
  });

  it("applies a choice to the document", async () => {
    renderTab();

    await userEvent.click(screen.getByRole("radio", { name: /Light/ }));

    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(screen.getByRole("radio", { name: /Light/ })).toBeChecked();
  });

  it("sends nothing to the backend", async () => {
    renderTab();

    await userEvent.click(screen.getByRole("radio", { name: /Light/ }));
    await userEvent.click(screen.getByRole("radio", { name: /Dark/ }));

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("says where the preference is kept, because it does not follow the account", async () => {
    renderTab();

    expect(screen.getByText(/this browser/i)).toBeInTheDocument();
  });

  it("says what auto currently resolves to", async () => {
    // "Auto" with no indication is the one option an operator cannot verify.
    renderTab();

    expect(screen.getByTestId("theme-resolved")).toHaveTextContent(/dark/i);
  });
});
