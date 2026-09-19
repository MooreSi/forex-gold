import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider, useTheme, THEME_KEY, THEMES } from "../ThemeContext";

/**
 * Light, dark, or follow the machine.
 *
 * A display preference, so it lives in the browser rather than in the trading
 * database: it is per-screen, it must apply before the first paint, and a
 * round-trip to the backend to find out what colour the page is would show a
 * flash of the wrong one every load.
 *
 * The tests are mostly about the failure modes of that storage. `localStorage`
 * throws in a private window and can come back holding anything at all, and a
 * dashboard that cannot read a colour preference must still render.
 *
 * **This repository's jsdom provides no `localStorage` at all** -- checked,
 * 2026-09-19 -- so the absent case is the DEFAULT here rather than an exotic
 * one, and every test that wants storage installs it. That is why the fake
 * below exists instead of `vi.spyOn(Storage.prototype, ...)`: there is no
 * `Storage` to spy on.
 */
function fakeStorage(over: Partial<Storage> = {}) {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
    key: () => null,
    get length() { return map.size; },
    ...over,
  } as Storage;
}
function Probe() {
  const { theme, resolved, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <span data-testid="resolved">{resolved}</span>
      {THEMES.map((t) => (
        <button key={t} onClick={() => setTheme(t)}>{t}</button>
      ))}
    </div>
  );
}

const renderProbe = () => render(<ThemeProvider><Probe /></ThemeProvider>);

function stubMatchMedia(prefersDark: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("dark") ? prefersDark : !prefersDark,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

let store: Storage;

beforeEach(() => {
  store = fakeStorage();
  vi.stubGlobal("localStorage", store);
  stubMatchMedia(true);
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => vi.unstubAllGlobals());

describe("what it starts as", () => {
  it("follows the machine until told otherwise", async () => {
    renderProbe();

    expect(screen.getByTestId("theme")).toHaveTextContent("auto");
  });

  it("remembers what was chosen last time", async () => {
    store.setItem(THEME_KEY, "light");

    renderProbe();

    expect(screen.getByTestId("theme")).toHaveTextContent("light");
  });

  it("ignores a stored value that is not a theme", async () => {
    // Whatever else wrote to this key, the page still has to render.
    store.setItem(THEME_KEY, "solarized-banana");

    renderProbe();

    expect(screen.getByTestId("theme")).toHaveTextContent("auto");
  });

  it("renders when storage cannot be read at all", async () => {
    // A private window, or site data blocked. Throwing here would blank the
    // whole dashboard, because this provider wraps it.
    vi.stubGlobal("localStorage", fakeStorage({
      getItem: () => { throw new Error("access denied"); },
    }));

    renderProbe();

    expect(screen.getByTestId("theme")).toHaveTextContent("auto");
  });

  it("renders when there is no storage object at all", async () => {
    // Not hypothetical: this repository's jsdom is exactly this case.
    vi.stubGlobal("localStorage", undefined);

    renderProbe();

    expect(screen.getByTestId("theme")).toHaveTextContent("auto");
  });
});

describe("choosing one", () => {
  it("puts the choice on the document, which is what the CSS reads", async () => {
    renderProbe();

    await userEvent.click(screen.getByRole("button", { name: "light" }));

    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("stores the choice", async () => {
    renderProbe();

    await userEvent.click(screen.getByRole("button", { name: "dark" }));

    expect(store.getItem(THEME_KEY)).toBe("dark");
  });

  it("keeps working when the choice cannot be stored", async () => {
    vi.stubGlobal("localStorage", fakeStorage({
      setItem: () => { throw new Error("quota exceeded"); },
    }));
    renderProbe();

    await userEvent.click(screen.getByRole("button", { name: "light" }));

    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });
});

describe("what auto resolves to", () => {
  it("is dark when the machine is dark", async () => {
    stubMatchMedia(true);

    renderProbe();

    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("is light when the machine is light", async () => {
    stubMatchMedia(false);

    renderProbe();

    expect(screen.getByTestId("resolved")).toHaveTextContent("light");
  });

  it("is dark when the machine will not say", async () => {
    // Darkness is this app's default and the one its colours were designed
    // against. An unanswered query must not flip a trading screen white.
    vi.stubGlobal("matchMedia", undefined);

    renderProbe();

    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("reports an explicit choice as itself, not as the machine's", async () => {
    stubMatchMedia(true);
    renderProbe();

    await userEvent.click(screen.getByRole("button", { name: "light" }));

    expect(screen.getByTestId("resolved")).toHaveTextContent("light");
  });
});
