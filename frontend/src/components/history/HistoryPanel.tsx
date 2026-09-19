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
import { TradeTableSection } from "./internal/TradeTableSection";

const SUB_TABS = [
  { id: "hours", label: "When it trades" },
  { id: "channels", label: "Channels" },
  { id: "ladder", label: "Ladder reach" },
  // Last, and not the default: the tab opens on the heatmap, which
  // HistoryPanel.test.tsx pins. This one is a row per trade and costs a
  // request of its own, so it loads when it is asked for.
  { id: "trades", label: "Trades" },
];

export function HistoryPanel() {
  const c = useHistoryController();

  return (
    <PanelShell
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
            <Tabs.Content value="hours">
              <HeatmapSection cells={c.hourly} />
            </Tabs.Content>
            <Tabs.Content value="channels">
              <ChannelsScorecard channels={c.channels} onPause={c.setChannelPaused} />
            </Tabs.Content>
            <Tabs.Content value="ladder">
              <LadderSection ladder={asObject(c.state.data.ladder)} />
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
