import type { ReactNode } from "react";
import { ExternalLink } from "lucide-react";
import { Tooltip } from "@/components/shared/Tooltip";
import type { ChartView } from "../hooks/useChartView";
import { ChartViewToggle } from "./ChartViewToggle";
import { TRADINGVIEW_FULL_CHART } from "./TradingViewChart";

/**
 * The Chart tab's header controls: the view switch, then the controls for
 * whichever view is showing.
 *
 * The broker chart's toolbar is hidden under TradingView, which has its own
 * timeframe and history controls; ours would sit beside it and do nothing.
 */
export function ChartHeaderActions({
  view, onView, toolbar,
}: { view: ChartView; onView: (v: ChartView) => void; toolbar: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <ChartViewToggle view={view} onView={onView} />
      {view === "tradingview" ? (
        <Tooltip
          label="The full chart on tradingview.com. Log in there and your drawings are kept; this embedded chart cannot keep them."
          side="bottom"
        >
          <a
            href={TRADINGVIEW_FULL_CHART}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-ink-2 hover:bg-surface-2 hover:text-ink-1"
          >
            <ExternalLink size={12} /> Open in TradingView
          </a>
        </Tooltip>
      ) : (
        toolbar
      )}
    </div>
  );
}
