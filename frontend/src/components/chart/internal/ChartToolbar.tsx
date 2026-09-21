import { RefreshCw } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import { TIMEFRAMES, type Timeframe } from "../hooks/useChartController";

interface ChartToolbarProps {
  timeframe: Timeframe;
  onTimeframe: (tf: Timeframe) => void;
  count: number;
  onCount: (n: number) => void;
  onRefresh: () => void;
}

const COUNTS = [100, 200, 300, 500];

/** A timeframe in words, for the hover help. `M5` says nothing to somebody who
 *  has not used MetaTrader. */
const TIMEFRAME_WORDS: Record<string, string> = {
  M1: "minute", M5: "5 minutes", M15: "15 minutes", M30: "30 minutes",
  H1: "hour", H4: "4 hours", D1: "day", W1: "week", MN1: "month",
};

export function ChartToolbar({
  timeframe, onTimeframe, count, onCount, onRefresh,
}: ChartToolbarProps) {
  return (
    <div className="flex items-center gap-1">
      {TIMEFRAMES.map((tf) => (
        <Tooltip key={tf} label={`One candle per ${TIMEFRAME_WORDS[tf] ?? tf}.`} side="bottom">
          <button
            onClick={() => onTimeframe(tf)}
            aria-pressed={tf === timeframe}
            className={cn(
              "num rounded px-2 py-1 text-[11px] transition-colors",
              tf === timeframe
                ? "bg-surface-3 text-ink-1"
                : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
            )}
          >
            {tf}
          </button>
        </Tooltip>
      ))}
      <Tooltip
        label="How many candles to load. More history costs a slower load and a smaller chart."
        side="bottom"
      >
      <select
        aria-label="Candles shown"
        value={count}
        onChange={(e) => onCount(Number(e.target.value))}
        className="num ml-2 rounded border border-line bg-surface-2 px-2 py-1 text-[11px] text-ink-2"
      >
        {COUNTS.map((n) => (
          <option key={n} value={n}>{n} bars</option>
        ))}
      </select>
      </Tooltip>
      <Button variant="ghost" onClick={onRefresh} title="Refresh now">
        <RefreshCw size={13} />
      </Button>
    </div>
  );
}
