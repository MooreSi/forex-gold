import { PanelShell } from "@/components/shared/PanelShell";
import { formatClock } from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import { useMarketResearch } from "@/components/ai/hooks/useMarketResearch";
import type { Candle } from "@/api/types";
import { DASHBOARD_DAYS, useDashboardController } from "./hooks/useDashboardController";
import { AiInsightsCard } from "./internal/AiInsightsCard";
import { AutomationCard } from "./internal/AutomationCard";
import { EquityCard } from "./internal/EquityCard";
import { MarketChartCard } from "./internal/MarketChartCard";
import { MarketIntelligenceCard } from "./internal/MarketIntelligenceCard";
import { OpenPositionsCard } from "./internal/OpenPositionsCard";
import { PerformanceCard } from "./internal/PerformanceCard";
import { PriceHeroCard } from "./internal/PriceHeroCard";
import { RiskExecutionCard } from "./internal/RiskExecutionCard";
import { SignalFeedCard } from "./internal/SignalFeedCard";

/**
 * The Dashboard: everything worth knowing at a glance, on one screen.
 *
 * Asked for on 2026-09-22 — a single screen so the operator does not have to
 * move between tabs to see what is happening. It is a READ-ONLY summary by
 * design. Every card names the tab that owns what it shows and offers no
 * control of its own: a close button, a lot-size box or a Start Engine switch
 * on a summary screen is one misclick from something that costs money, and
 * the tabs that do own those controls ask for confirmation first.
 *
 * Composition only. The polls are in the controller hook — which opens no new
 * poll key, so this tab costs exactly what the tabs it summarises cost — and
 * the JSX is in `internal/`.
 */
export function DashboardPanel() {
  const c = useDashboardController();
  // Free: the last stored analysis, never a new model call. See AiInsightsCard.
  const research = useMarketResearch();

  const header = c.header.data;
  const updatedAt = c.header.updatedAt;

  return (
    <PanelShell
      icon="activity"
      title="Dashboard"
      subtitle={updatedAt
        ? `live · updated ${formatClock(updatedAt / 1000)}`
        : "waiting for the first read"}
    >
      <div className="space-y-3">
        <PriceHeroCard
          tick={header?.tick ?? null}
          header={header}
          daily={asArray<Candle>(c.daily.data)}
        />

        <div className="grid gap-3 xl:grid-cols-3">
          <div className="space-y-3 xl:col-span-2">
            <MarketChartCard
              candles={asArray<Candle>(c.chart.candles.data)}
              overlays={c.chart.overlays.data}
              tick={header?.tick ?? null}
              trades={c.chartTrades}
              timeframe={c.chart.timeframe}
              onTimeframe={c.chart.setTimeframe}
              error={c.chart.candles.error}
            />
            <div className="grid gap-3 md:grid-cols-2">
              <PerformanceCard performance={c.performance} days={DASHBOARD_DAYS} />
              <EquityCard poll={c.closed} days={DASHBOARD_DAYS} />
            </div>
            <MarketIntelligenceCard
              setforget={c.setforget.data}
              news={c.news.data}
              markets={c.markets}
            />
          </div>

          <div className="space-y-3">
            <OpenPositionsCard trades={c.positions} />
            <SignalFeedCard signals={c.signals} />
            <RiskExecutionCard risk={c.risk.data ?? {}} header={header} />
            <AiInsightsCard
              setforget={c.setforget.data}
              analysis={research.analysis}
              savedAt={research.savedAt}
            />
            <AutomationCard engines={c.engines} />
          </div>
        </div>
      </div>
    </PanelShell>
  );
}
