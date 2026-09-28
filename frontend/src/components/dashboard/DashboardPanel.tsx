import { PanelShell } from "@/components/shared/PanelShell";
import { formatClock } from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import { useMarketResearch } from "@/components/ai/hooks/useMarketResearch";
import type { Candle } from "@/api/types";
import { DASHBOARD_DAYS, useDashboardController } from "./hooks/useDashboardController";
import { AiInsightsCard } from "./internal/AiInsightsCard";
import { AutomationCard } from "./internal/AutomationCard";
import { BrainCard } from "./internal/BrainCard";
import { EquityCard } from "./internal/EquityCard";
import { FillCostCard } from "./internal/FillCostCard";
import { GexCard } from "./internal/GexCard";
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
      {/* Rows, not two free-running columns: each row's cards share a
          height, so the screen ends level instead of one column trailing a
          long way below the other. Read top to bottom it goes price, what
          is open, how it has gone, why the app did what it did, context. */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-12">
        <div className="xl:col-span-12">
          <PriceHeroCard
            tick={header?.tick ?? null}
            header={header}
            daily={asArray<Candle>(c.daily.data)}
          />
        </div>

        <div className="xl:col-span-8">
          <MarketChartCard
            candles={asArray<Candle>(c.chart.candles.data)}
            overlays={c.chart.overlays.data}
            tick={header?.tick ?? null}
            trades={c.chartTrades}
            timeframe={c.chart.timeframe}
            onTimeframe={c.chart.setTimeframe}
            error={c.chart.candles.error}
          />
        </div>
        <div className="flex flex-col gap-3 xl:col-span-4">
          <div><OpenPositionsCard trades={c.positions} /></div>
          <div><AutomationCard engines={c.engines} /></div>
          <div className="flex-1">
            <SignalFeedCard signals={c.signals} />
          </div>
        </div>

        {/* Risk & execution gets the widest slot: four limits and three
            connection states do not fit a third of a laptop screen. */}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:col-span-12 xl:grid-cols-12">
          <div className="xl:col-span-4">
            <PerformanceCard performance={c.performance} days={DASHBOARD_DAYS} />
          </div>
          <div className="xl:col-span-3">
            <EquityCard poll={c.closed} days={DASHBOARD_DAYS} />
          </div>
          <div className="md:col-span-2 xl:col-span-5">
            <RiskExecutionCard risk={c.risk.data ?? {}} header={header} />
          </div>
        </div>

        <div className="xl:col-span-12">
          <BrainCard />
        </div>

        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:col-span-12 xl:grid-cols-3">
          <MarketIntelligenceCard
            setforget={c.setforget.data}
            news={c.news.data}
            markets={c.markets}
          />
          <AiInsightsCard
            setforget={c.setforget.data}
            analysis={research.analysis}
            savedAt={research.savedAt}
          />
          <div className="md:col-span-2 xl:col-span-1">
            <GexCard />
          </div>
        </div>

        <div className="xl:col-span-12">
          <FillCostCard />
        </div>
      </div>
    </PanelShell>
  );
}
