import { useCallback, useState } from "react";

export type ChartView = "broker" | "tradingview";

const KEY = "chart-view";

/**
 * Which chart the Chart tab shows, remembered per browser.
 *
 * `localStorage` can throw -- private windows, blocked site data -- and losing
 * the preference is fine; losing the tab to an exception is not.
 */
export function useChartView() {
  const [view, setViewState] = useState<ChartView>(() => {
    try {
      return localStorage.getItem(KEY) === "tradingview" ? "tradingview" : "broker";
    } catch {
      return "broker";
    }
  });

  const setView = useCallback((next: ChartView) => {
    setViewState(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // The view still changes; it just is not remembered.
    }
  }, []);

  return { view, setView };
}
