import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import type { ChartView } from "../hooks/useChartView";

const VIEWS: { id: ChartView; label: string; help: string }[] = [
  {
    id: "broker",
    label: "Broker",
    help: "Your broker's XAUUSD prices from MT5, with your open trades and fair-value gaps.",
  },
  {
    id: "tradingview",
    label: "TradingView",
    help: "TradingView's chart: any instrument, years of history, drawing tools. Its prices are TradingView's, not your broker's.",
  },
];

export function ChartViewToggle({
  view, onView,
}: { view: ChartView; onView: (v: ChartView) => void }) {
  return (
    <div className="flex items-center gap-1 rounded border border-line p-0.5">
      {VIEWS.map((v) => (
        <Tooltip key={v.id} label={v.help} side="bottom">
          <button
            onClick={() => onView(v.id)}
            aria-pressed={v.id === view}
            className={cn(
              "rounded px-2 py-0.5 text-[11px] transition-colors",
              v.id === view
                ? "bg-surface-3 text-ink-1"
                : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
            )}
          >
            {v.label}
          </button>
        </Tooltip>
      ))}
    </div>
  );
}
