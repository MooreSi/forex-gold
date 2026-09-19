import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react";

/**
 * Light, dark, or follow the machine.
 *
 * **A display preference, so it lives in the browser, not the trading
 * database.** It is per-screen, it has to apply before the first paint, and a
 * round-trip to the backend to find out what colour the page is would show a
 * flash of the wrong one on every load. Everything in `app_config` is
 * something the engines read; this is not.
 *
 * The choice is written to `<html data-theme>` and the stylesheet does the
 * rest — see `index.css`, where only the neutral scale changes. Profit green,
 * loss red and warning amber are frozen across both themes, because those are
 * money semantics rather than decoration.
 *
 * Every `localStorage` access is wrapped: it throws in a private window and it
 * can come back holding anything at all. This provider wraps the whole
 * dashboard, so an exception here would blank the app over a colour.
 */
export const THEMES = ["auto", "light", "dark"] as const;
export type Theme = (typeof THEMES)[number];

export const THEME_KEY = "forex.theme";

/** What `auto` falls back to when the machine will not say.
 *
 *  Dark, deliberately. It is the theme this app's colours were designed
 *  against, and an unanswered media query must not flip a trading screen
 *  white mid-session. */
const FALLBACK: "light" | "dark" = "dark";

interface ThemeState {
  theme: Theme;
  /** "light" or "dark" — what `auto` actually came out as. */
  resolved: "light" | "dark";
  setTheme: (t: Theme) => void;
}

const ThemeContext = createContext<ThemeState | null>(null);

function isTheme(value: unknown): value is Theme {
  return typeof value === "string" && (THEMES as readonly string[]).includes(value);
}

function stored(): Theme {
  try {
    const raw = localStorage.getItem(THEME_KEY);
    return isTheme(raw) ? raw : "auto";
  } catch {
    return "auto";
  }
}

function machinePrefers(): "light" | "dark" {
  try {
    if (typeof matchMedia !== "function") return FALLBACK;
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return FALLBACK;
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(stored);
  const [machine, setMachine] = useState<"light" | "dark">(machinePrefers);

  // Follow the machine while it is being followed. Without this, "auto" is
  // only auto at the moment the page loaded.
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    let query: MediaQueryList;
    try {
      query = matchMedia("(prefers-color-scheme: dark)");
    } catch {
      return;
    }
    const onChange = () => setMachine(query.matches ? "dark" : "light");
    query.addEventListener?.("change", onChange);
    return () => query.removeEventListener?.("change", onChange);
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // The colour still changes. Losing the preference between sessions is
      // a smaller failure than refusing to change it at all.
    }
  }, []);

  const value = useMemo<ThemeState>(() => ({
    theme,
    resolved: theme === "auto" ? machine : theme,
    setTheme,
  }), [theme, machine, setTheme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside a ThemeProvider");
  return ctx;
}
