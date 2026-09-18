import { RefreshCw } from "lucide-react";
import { Button } from "@/components/shared/Button";
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

export function ChartToolbar({
  timeframe, onTimeframe, count, onCount, onRefresh,
}: ChartToolbarProps) {
  return (
    <div className="flex items-center gap-1">
      {TIMEFRAMES.map((tf) => (
        <button
          key={tf}
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
      ))}
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
      <Button variant="ghost" onClick={onRefresh} title="Refresh now">
        <RefreshCw size={13} />
      </Button>
    </div>
  );
}
