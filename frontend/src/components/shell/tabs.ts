/**
 * The tab strip. Data, not JSX, so a test can assert the anatomy without
 * rendering anything, and so the "not ported yet" entries carry the task that
 * will fill them.
 *
 * The last ten are the NiceGUI shell's own tabs, in its order and with its
 * names (`frontend/app/__init__.py:411-420`). That order is not cosmetic: it
 * is the order the operator has learned, so a new tab goes in front of it
 * rather than into the middle of it.
 *
 * Dashboard was added on 2026-09-22 at the owner's request -- one screen that
 * answers "what is happening right now" without moving between the ten. It
 * summarises them and controls none of them.
 */
export interface TabSpec {
  id: string;
  label: string;
  icon: string;
  /** Null once the tab is a real React panel. */
  notPorted: { task: string; origin: string } | null;
}

export const TABS: TabSpec[] = [
  { id: "dashboard", label: "Dashboard", icon: "activity", notPorted: null },
  { id: "ai", label: "AI Analysis", icon: "bot", notPorted: null },
  { id: "chart", label: "Chart", icon: "candlestick", notPorted: null },
  { id: "trading", label: "Trading", icon: "trending-up", notPorted: null },
  { id: "parsing", label: "Parsing", icon: "send", notPorted: null },
  { id: "generator", label: "Signal Generator", icon: "flask", notPorted: null },
  { id: "backtest", label: "Backtest", icon: "bar-chart", notPorted: null },
  { id: "analysis", label: "Analysis", icon: "history", notPorted: null },
  { id: "settings", label: "Settings", icon: "settings", notPorted: null },
  { id: "news", label: "News", icon: "newspaper", notPorted: null },
  { id: "about", label: "About", icon: "info", notPorted: null },
];

/**
 * Where the app opens.
 *
 * Chart under NiceGUI and for the whole React port; **Dashboard from
 * 2026-09-22**, at the owner's request. The Dashboard is the summary of every
 * other tab and it is read-only -- it has no control that can place, close or
 * size anything -- so landing on it shows the most and risks the least.
 */
export const DEFAULT_TAB = "dashboard";
