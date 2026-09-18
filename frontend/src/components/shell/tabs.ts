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
  {
    id: "ai", label: "AI Analysis", icon: "bot",
    notPorted: { task: "080-remaining-tabs", origin: "frontend/pages/ai_trade_analysis/" },
  },
  { id: "chart", label: "Chart", icon: "candlestick", notPorted: null },
  { id: "trading", label: "Trading", icon: "trending-up", notPorted: null },
  {
    id: "parsing", label: "Parsing", icon: "send",
    notPorted: { task: "080-remaining-tabs", origin: "frontend/pages/telegram/" },
  },
  {
    id: "generator", label: "Signal Generator", icon: "flask",
    notPorted: { task: "080-remaining-tabs", origin: "frontend/pages/test_panel/" },
  },
  {
    id: "backtest", label: "Backtest", icon: "bar-chart",
    notPorted: { task: "080-remaining-tabs", origin: "frontend/pages/backtest/" },
  },
  {
    id: "analysis", label: "Analysis", icon: "history",
    notPorted: { task: "080-remaining-tabs", origin: "frontend/pages/history/" },
  },
  {
    id: "settings", label: "Settings", icon: "settings",
    notPorted: { task: "080-remaining-tabs", origin: "frontend/pages/settings/" },
  },
  {
    id: "news", label: "News", icon: "newspaper",
    notPorted: { task: "080-remaining-tabs", origin: "frontend/pages/news.py" },
  },
  {
    id: "about", label: "About", icon: "info",
    notPorted: { task: "080-remaining-tabs", origin: "frontend/app/_about.py" },
  },
];

/** Chart is where the app opens, as it did under NiceGUI. */
export const DEFAULT_TAB = "chart";
