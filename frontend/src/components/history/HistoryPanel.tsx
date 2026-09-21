import * as Tabs from "@radix-ui/react-tabs";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { cn } from "@/lib/cn";
import { asObject } from "@/lib/asArray";
import { useHistoryController, WINDOWS } from "./hooks/useHistoryController";
import { ChannelsScorecard } from "./internal/ChannelsScorecard";
import { HeatmapSection } from "./internal/HeatmapSection";
import { LadderSection } from "./internal/LadderSection";
import { PerformanceSection } from "./internal/PerformanceSection";
import { CalendarSection } from "./internal/CalendarSection";
import { DpmSection } from "./internal/DpmSection";
import { EquityCurveSection } from "./internal/EquityCurveSection";
import { TradeAnalysisPanel } from "@/components/ai/TradeAnalysisPanel";
import { TradeTableSection } from "./internal/TradeTableSection";

const SUB_TABS = [
  // First, and still not the default: the tab opens on the heatmap, which
  // HistoryPanel.test.tsx pins. Radix mounts a tab's content when it is
  // selected, so listing this one first costs nothing until it is asked for
  // -- it is a row per trade and a request of its own. Owner, 2026-09-21:
  // the per-trade list is what the page is opened for, so it reads first.
  { id: "trades", label: "Trades" },
  { id: "equity", label: "Equity curve" },
  { id: "calendar", label: "Calendar" },
  { id: "hours", label: "When it trades" },
  { id: "channels", label: "Channels" },
  { id: "ladder", label: "Ladder reach" },
  { id: "dpm", label: "DPM" },
  // Where the NiceGUI app had it: `frontend/pages/history/__init__.py`
  // renders `ai_trade_analysis` inside this tab. The React port mounted it on
  // the AI Analysis tab instead, which left the tab named for the market
  // research showing something else entirely.
  { id: "ai", label: "AI trade analysis" },
];

export function HistoryPanel() {
  const c = useHistoryController();

  return (
    <PanelShell
      icon="history"
      title="Analysis"
      subtitle={`last ${c.days} days`}
      actions={
        <>
          {WINDOWS.map((d) => (
            <button
              key={d}
              onClick={() => c.setDays(d)}
              aria-pressed={d === c.days}
              className={cn(
                "num rounded px-2 py-1 text-[11px] transition-colors",
                d === c.days
                  ? "bg-surface-3 text-ink-1"
                  : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
              )}
            >
              {d}d
            </button>
          ))}
          <Button
            variant="ghost"
            onClick={() => void c.recompute()}
            disabled={c.recomputing}
            title="Rebuild the channel scorecard from every trade in this window"
          >
            <RefreshCw size={13} />
          </Button>
        </>
      }
    >
      {!c.state.data ? (
        <EmptyState
          title={c.state.error ? "Could not load the analysis" : "Loading"}
          hint={c.state.error?.message}
        />
      ) : (
        <div className="space-y-4">
          <PerformanceSection performance={asObject(c.state.data.performance)} />
          <Tabs.Root defaultValue="hours" className="flex min-h-0 flex-1 flex-col">
            <Tabs.List className="mb-3 flex gap-1 border-b border-line">
              {SUB_TABS.map((t) => (
                <Tabs.Trigger
                  key={t.id}
                  value={t.id}
                  className={cn(
                    "-mb-px border-b-2 px-3 py-1.5 text-xs transition-colors",
                    "border-transparent text-ink-3 hover:text-ink-2",
                    "data-[state=active]:border-accent data-[state=active]:text-ink-1",
                  )}
                >
                  {t.label}
                </Tabs.Trigger>
              ))}
            </Tabs.List>
            <Tabs.Content value="equity">
              {/* Shares the trades poll with the table below, so opening both
                  costs one request rather than two. */}
              <EquityCurveSection days={c.days} />
            </Tabs.Content>
            <Tabs.Content value="calendar">
              {/* Shares the trades poll with the curve and the table, so the
                  three cannot disagree about what a day earned. */}
              <CalendarSection days={c.days} />
            </Tabs.Content>
            <Tabs.Content value="hours">
              <HeatmapSection cells={c.hourly} />
            </Tabs.Content>
            <Tabs.Content value="channels">
              <ChannelsScorecard channels={c.channels} onPause={c.setChannelPaused} />
            </Tabs.Content>
            <Tabs.Content value="ladder">
              <LadderSection ladder={asObject(c.state.data.ladder)} />
            </Tabs.Content>
            <Tabs.Content value="dpm">
              {/* The sixth NiceGUI sub-tab. /api/ai/dpm has served these three
                  tables since the port and nothing rendered them. */}
              <DpmSection />
            </Tabs.Content>
            <Tabs.Content value="ai">
              {/* Reads its evidence for free; each section has its own Ask
                  button, so opening this sub-tab bills nothing. */}
              <TradeAnalysisPanel />
            </Tabs.Content>
            <Tabs.Content value="trades">
              {/* Its own endpoint, not a field on /state: this is a row per
                  trade and the rest of the tab is aggregates. A window nobody
                  is looking at should not be paying for it. */}
              <TradeTableSection days={c.days} />
            </Tabs.Content>
          </Tabs.Root>
        </div>
      )}
    </PanelShell>
  );
}
