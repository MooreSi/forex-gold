import { PanelShell } from "@/components/shared/PanelShell";
import { asArray } from "@/lib/asArray";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatPrice } from "@/components/shared/format";
import type { Candle, Trade } from "@/api/types";
import { useChartController } from "./hooks/useChartController";
import { CandleChart } from "./internal/CandleChart";
import { ChartToolbar } from "./internal/ChartToolbar";
import { ChartTradesSection } from "./internal/ChartTradesSection";

/** Thin wrapper: composition and nothing else. State is in the controller
 *  hook, JSX is in internal/. */
export function ChartPanel() {
  const c = useChartController();
  const candles = asArray<Candle>(c.candles.data);
  const trades = asArray<Trade>(c.trades.data);

  return (
    <div className="grid h-full min-h-0 gap-3 lg:grid-cols-[minmax(0,1fr)_20rem]">
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
        className="min-h-[24rem]"
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

      <PanelShell title="Open positions" subtitle="drawn on the chart" icon="positions">
        <ChartTradesSection trades={trades} />
      </PanelShell>
    </div>
  );
}
