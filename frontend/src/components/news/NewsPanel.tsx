import { RefreshCw } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { asObject } from "@/lib/asArray";
import type { NewsEvent } from "@/api/types";
import { useNewsController } from "./hooks/useNewsController";
import { BlackoutSection } from "./internal/BlackoutSection";
import { EventsSection } from "./internal/EventsSection";
import { NewsBanner } from "./internal/NewsBanner";

/**
 * The next release the blackout would actually hold entries for.
 *
 * `events[0]` was used, which is simply the next row on the calendar whatever
 * its impact. On the running app that put a bank holiday and a LOW-impact
 * house-price index under the words "Next high impact" — neither of which
 * holds an entry.
 */
function nextHighImpact(events: NewsEvent[]): NewsEvent | undefined {
  return events.find((e) => String(e.impact ?? "").toLowerCase() === "high");
}

export function NewsPanel() {
  const c = useNewsController();
  const data = c.state.data;

  return (
    <PanelShell
      icon="newspaper"
      title="Economic calendar"
      subtitle="times in UTC"
      actions={
        <>
          <label className="flex items-center gap-1.5 text-[11px] text-ink-2">
            <Tooltip label="Hide releases in currencies that do not move gold. A display filter only — the blackout below still reads the whole calendar, so hiding an event here does not stop it holding the engines.">
              <input
                type="checkbox"
                checked={c.goldOnly}
                aria-label="Gold-relevant currencies only"
                onChange={(e) => c.setGoldOnly(e.target.checked)}
                className="accent-accent"
              />
            </Tooltip>
            Gold only
          </label>
          <label className="flex items-center gap-1.5 text-[11px] text-ink-2">
            <Tooltip label="Hide releases that have already happened. Also a display filter — one that has just passed may still be inside the blackout's minutes-after window.">
              <input
                type="checkbox"
                checked={c.upcomingOnly}
                aria-label="Upcoming only"
                onChange={(e) => c.setUpcomingOnly(e.target.checked)}
                className="accent-accent"
              />
            </Tooltip>
            Upcoming only
          </label>
          <Button variant="ghost" onClick={() => void c.refresh()} title="Re-fetch the calendar"
            tooltip="Drop the cached calendar and fetch it again. The calendar is cached because the provider rate-limits it, so this is for when a release has been revised.">
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
          <NewsBanner current={data.current} next={nextHighImpact(c.events)} />
          <BlackoutSection settings={asObject(data.blackout)} onSave={c.saveBlackout} />
          <EventsSection events={c.events} />
        </div>
      )}
    </PanelShell>
  );
}
