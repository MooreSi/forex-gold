import { PanelShell } from "@/components/shared/PanelShell";
import { SplitPane } from "@/components/shared/SplitPane";
import { asArray } from "@/lib/asArray";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatPrice } from "@/components/shared/format";
import type { Candle, Trade } from "@/api/types";
import { useChartController } from "./hooks/useChartController";
import { CandleChart } from "./CandleChart";
import { ChartToolbar } from "./internal/ChartToolbar";
import { ChartTradesSection } from "./internal/ChartTradesSection";

/**
 * Thin wrapper: composition and nothing else. State is in the controller
 * hook, JSX is in internal/.
 *
 * The chart and the positions table are separated by a divider the operator
 * can drag, and the table starts with a third of the width rather than a fixed
 * 20rem. Asked for on 2026-09-21: "reduce the width of the chart and increase
 * the width of the open positions table so the detail within the open
 * positions can be viewed easily, may be easier to make the tables adjustable
 * on the screen". Seven columns in 20rem wrapped every one of them.
 */
export function ChartPanel() {
  const c = useChartController();
  const candles = asArray<Candle>(c.candles.data);
  const trades = asArray<Trade>(c.trades.data);

  return (
    <SplitPane
      storageKey="chart-positions-split"
      defaultRightPct={33}
      minRightPct={18}
      maxRightPct={65}
      left={
      <PanelShell
        icon="candlestick"
        title="XAUUSD"
        subtitle={c.tick.data ? `spread ${formatPrice(c.tick.data.spread, 2)}` : "waiting for a price"}
        actions={
          <ChartToolbar
            timeframe={c.timeframe}
            onTimeframe={c.setTimeframe}
            count={c.count}
            onCount={c.setCount}
            onRefresh={() => void c.refreshAll()}
          />
        }
        // `flex-1` is what claims the slot's height; `min-h-[24rem]` is the
        // floor on a short window. Without the first of those the panel sits at
        // content height and the canvas inside it collapses to its time axis.
        className="min-h-[24rem] flex-1"
      >
        {candles.length === 0 ? (
          <EmptyState
            title={c.candles.error ? "Could not load candles" : "Waiting for candles"}
            hint={
              c.candles.error
                ? c.candles.error.message
                : "The MT5 bridge supplies these. Check the bridge indicator in the header if this stays empty."
            }
          />
        ) : (
          <CandleChart
            candles={candles}
            overlays={c.overlays.data}
            tick={c.tick.data ?? null}
            trades={trades}
          />
        )}
      </PanelShell>
      }
      right={
        <PanelShell
          title="Open positions"
          subtitle="drawn on the chart"
          icon="positions"
          className="min-h-0 flex-1"
        >
          <ChartTradesSection trades={trades} />
        </PanelShell>
      }
    />
  );
}
