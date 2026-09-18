import { RefreshCw } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { asObject } from "@/lib/asArray";
import { useNewsController } from "./hooks/useNewsController";
import { BlackoutSection } from "./internal/BlackoutSection";
import { EventsSection } from "./internal/EventsSection";
import { NewsBanner } from "./internal/NewsBanner";

export function NewsPanel() {
  const c = useNewsController();
  const data = c.state.data;

  return (
    <PanelShell
      title="Economic calendar"
      subtitle="times in UTC"
      actions={
        <>
          <label className="flex items-center gap-1.5 text-[11px] text-ink-2">
            <input
              type="checkbox"
              checked={c.goldOnly}
              aria-label="Gold-relevant currencies only"
              onChange={(e) => c.setGoldOnly(e.target.checked)}
              className="accent-accent"
            />
            Gold only
          </label>
          <label className="flex items-center gap-1.5 text-[11px] text-ink-2">
            <input
              type="checkbox"
              checked={c.upcomingOnly}
              aria-label="Upcoming only"
              onChange={(e) => c.setUpcomingOnly(e.target.checked)}
              className="accent-accent"
            />
            Upcoming only
          </label>
          <Button variant="ghost" onClick={() => void c.refresh()} title="Re-fetch the calendar">
            <RefreshCw size={13} />
          </Button>
        </>
      }
    >
      {!data ? (
        <EmptyState
          title={c.state.error ? "Could not load the calendar" : "Loading the calendar"}
          hint={c.state.error?.message}
        />
      ) : (
        <div className="space-y-4">
          <NewsBanner current={data.current} next={c.events[0]} />
          <BlackoutSection settings={asObject(data.blackout)} onSave={c.saveBlackout} />
          <EventsSection events={c.events} />
        </div>
      )}
    </PanelShell>
  );
}
