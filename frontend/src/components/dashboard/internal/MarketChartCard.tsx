import type { Candle, Overlays, Tick, Trade } from "@/api/types";
import { CandleChart } from "@/components/chart/CandleChart";
import { EmptyState } from "@/components/shared/EmptyState";
import { cn } from "@/lib/cn";
import type { Timeframe } from "@/components/chart/hooks/useChartController";
import { DashCard } from "./DashCard";

/**
 * The timeframes this card offers.
 *
 * A subset of the Chart tab's seven, not a new vocabulary: the strings are
 * that tab's own `Timeframe` values, so the button lit here asks for exactly
 * what the Chart tab would ask for, on the same poll key. Four buttons is what
 * fits above a card this size; the tab is where the other three live.
 */
const TIMEFRAMES: Timeframe[] = ["5m", "15m", "1H", "4H", "1D"];

interface MarketChartCardProps {
  candles: Candle[];
  overlays: Overlays | null;
  tick: Tick | null;
  trades: Trade[];
  timeframe: Timeframe;
  onTimeframe: (tf: Timeframe) => void;
  error: Error | null;
}

/**
 * The candles, with the same canvas, overlays and position markers the Chart
 * tab draws.
 *
 * `CandleChart` is imported from the chart domain rather than reimplemented.
 * A second candlestick component would be a second answer to what an EMA or a
 * fair-value gap looks like, and this app has already paid once for two
 * implementations of one thing.
 */
export function MarketChartCard({
  candles, overlays, tick, trades, timeframe, onTimeframe, error,
}: MarketChartCardProps) {
  return (
    <DashCard
      title="XAUUSD"
      icon="candlestick"
      className="min-h-[22rem]"
      actions={
        <div className="flex items-center gap-0.5 rounded bg-surface-2 p-0.5">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => onTimeframe(tf)}
              aria-pressed={tf === timeframe}
              className={cn(
                "num rounded px-1.5 py-0.5 text-[10px] transition-colors",
                tf === timeframe
                  ? "bg-accent/20 text-accent"
                  : "text-ink-3 hover:bg-surface-3 hover:text-ink-2",
              )}
            >
              {tf.toUpperCase()}
            </button>
          ))}
        </div>
      }
      footnote="Open positions are drawn on the chart. The Chart tab has the full toolbar."
    >
      <div className="h-[18rem]">
        {candles.length === 0 ? (
          <EmptyState
            title={error ? "Could not load candles" : "Waiting for candles"}
            hint={error
              ? error.message
              : "The MT5 bridge supplies these. Check the bridge indicator in the header if this stays empty."}
          />
        ) : (
          <CandleChart candles={candles} overlays={overlays} tick={tick} trades={trades} />
        )}
      </div>
    </DashCard>
  );
}
