import { useCallback, useMemo, useState } from "react";

export const WINDOW_KEY = "dashboard-window";

/**
 * The windows the Dashboard's performance and equity cards offer.
 *
 * Today is today on the trading clock, from its midnight (owner, 2026-10-10).
 * It was "1 day", the last 24 hours, which on a Saturday morning read -$44.22
 * of Friday's trading. `today` asks the backend to count from that midnight;
 * `days` still says how far back to read.
 *
 * All time is 3650 days because `/api/history/*` validates `days` as
 * `le=3650` -- the router's comment says that is what the old "all" option
 * meant. Anything larger is a 422, which the cards would show as "no broker
 * data" for an account that has plenty.
 */
export const DASHBOARD_WINDOWS = [
  { days: 1, today: true, short: "Today", name: "Today", badge: "today" },
  { days: 7, today: false, short: "1W", name: "1 week", badge: "last 7 days" },
  { days: 30, today: false, short: "30D", name: "30 days", badge: "last 30 days" },
  { days: 3650, today: false, short: "All", name: "All time", badge: "all time" },
] as const;

export type DashboardWindow = (typeof DASHBOARD_WINDOWS)[number];

/** What the screen showed before the choice existed (2026-10-09). */
const DEFAULT_DAYS = 30;

function windowFor(days: number): DashboardWindow {
  return DASHBOARD_WINDOWS.find((w) => w.days === days)
    ?? DASHBOARD_WINDOWS.find((w) => w.days === DEFAULT_DAYS)!;
}

/**
 * Which window the Dashboard measures performance over, remembered per browser.
 *
 * A stored value that is not one of the offered windows falls back to the
 * default rather than reaching the backend as `days=abc`. `localStorage` can
 * throw -- private windows, blocked site data -- and losing the preference is
 * fine; losing the Dashboard to an exception is not.
 */
export function useDashboardWindow() {
  const [current, setCurrent] = useState<DashboardWindow>(() => {
    try {
      return windowFor(Number(localStorage.getItem(WINDOW_KEY)));
    } catch {
      return windowFor(DEFAULT_DAYS);
    }
  });

  const choose = useCallback((days: number) => {
    const next = windowFor(days);
    setCurrent(next);
    try {
      localStorage.setItem(WINDOW_KEY, String(next.days));
    } catch {
      // The window still changes; it just is not remembered.
    }
  }, []);

  return useMemo(() => ({ current, choose }), [current, choose]);
}
