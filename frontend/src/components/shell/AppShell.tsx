import * as Tabs from "@radix-ui/react-tabs";
import { useState, type ComponentType } from "react";
import { AppHeader } from "./AppHeader";
import { DEFAULT_TAB, TABS } from "./tabs";
import { ChartPanel } from "@/components/chart/ChartPanel";
import { TradingPanel } from "@/components/trading/TradingPanel";
import { NewsPanel } from "@/components/news/NewsPanel";
import { AboutPanel } from "@/components/about/AboutPanel";
import { BacktestPanel } from "@/components/backtest/BacktestPanel";
import { ParsingPanel } from "@/components/parsing/ParsingPanel";
import { HistoryPanel } from "@/components/history/HistoryPanel";
import { AiPanel } from "@/components/ai/AiPanel";
import { EnginesPanel } from "@/components/engines/EnginesPanel";
import { SettingsPanel } from "@/components/settings/SettingsPanel";
import { NotPortedPanel } from "@/components/shared/NotPortedPanel";
import { cn } from "@/lib/cn";

const PANELS: Record<string, ComponentType> = {
  chart: ChartPanel,
  trading: TradingPanel,
  news: NewsPanel,
  about: AboutPanel,
  backtest: BacktestPanel,
  parsing: ParsingPanel,
  analysis: HistoryPanel,
  ai: AiPanel,
  generator: EnginesPanel,
  settings: SettingsPanel,
};

/**
 * The shell: header, the ten tabs, and the panel for whichever is selected.
 *
 * A tab is rendered only while it is selected. The NiceGUI version built every
 * panel up front and left their timers running, which is how one unattended
 * browser tab came to stall the event loop for every other client.
 */
export function AppShell() {
  const [tab, setTab] = useState(DEFAULT_TAB);

  return (
    <div className="flex h-full flex-col bg-surface-0">
      <AppHeader />
      <Tabs.Root value={tab} onValueChange={setTab} className="flex min-h-0 flex-1 flex-col">
        <Tabs.List
          aria-label="Dashboard sections"
          className="flex shrink-0 gap-0.5 overflow-x-auto border-b border-line bg-surface-1 px-2"
        >
          {TABS.map((t) => (
            <Tabs.Trigger
              key={t.id}
              value={t.id}
              className={cn(
                "-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-xs transition-colors",
                "border-transparent text-ink-3 hover:text-ink-2",
                "data-[state=active]:border-accent data-[state=active]:text-ink-1",
              )}
            >
              {t.label}
              {t.notPorted && (
                <span className="ml-1.5 text-[9px] uppercase text-warning">wip</span>
              )}
            </Tabs.Trigger>
          ))}
        </Tabs.List>

        {TABS.map((t) => {
          const Panel = PANELS[t.id];
          return (
            <Tabs.Content
              key={t.id}
              value={t.id}
              className="min-h-0 flex-1 overflow-auto p-3"
              // Unmount on switch: see the note above about idle panels.
              forceMount={undefined}
            >
              {Panel ? (
                <Panel />
              ) : (
                <NotPortedPanel
                  tab={t.label}
                  task={t.notPorted?.task ?? "080-remaining-tabs"}
                  origin={t.notPorted?.origin ?? "frontend/"}
                />
              )}
            </Tabs.Content>
          );
        })}
      </Tabs.Root>
    </div>
  );
}
