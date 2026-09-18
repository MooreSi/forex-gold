/**
 * The ten tabs, in the order and with the names the NiceGUI shell used
 * (`frontend/app/__init__.py:411-420`). Data, not JSX, so a test can assert
 * the anatomy without rendering anything, and so the "not ported yet" entries
 * carry the task that will fill them.
 */
export interface TabSpec {
  id: string;
  label: string;
  icon: string;
  /** Null once the tab is a real React panel. */
  notPorted: { task: string; origin: string } | null;
}

export const TABS: TabSpec[] = [
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

/** Chart is where the app opens, as it did under NiceGUI. */
export const DEFAULT_TAB = "chart";
