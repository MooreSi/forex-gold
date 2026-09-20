import { TriangleAlert, CalendarClock } from "lucide-react";
import { formatUtcTime } from "@/components/shared/format";
import type { CurrentEvent, NewsEvent } from "@/api/types";

interface NewsBannerProps {
  current: CurrentEvent | null;
  next: NewsEvent | undefined;
}

function minutes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const whole = Math.max(0, Math.round(value));
  if (whole < 60) return `${whole}m`;
  return `${Math.floor(whole / 60)}h ${whole % 60}m`;
}

/**
 * Either "a blackout is running" or "here is the next one".
 *
 * Amber, not red: a blackout is the system working as configured, not a fault.
 * Red is reserved for loss and for disconnection.
 */
export function NewsBanner({ current, next }: NewsBannerProps) {
  if (current) {
    return (
      <div
        role="status"
        data-testid="news-banner"
        data-state="blackout"
        className="flex items-center gap-3 rounded border border-warning/40 bg-warning/10 px-3 py-2"
      >
        <TriangleAlert className="text-warning" size={16} />
        <div className="min-w-0">
          <p className="text-xs font-semibold text-warning">Blackout active</p>
          <p className="truncate text-xs text-ink-2">
            {current.title} ({current.currency})
          </p>
        </div>
        <span className="num ml-auto text-xs text-ink-2">
          resumes in {minutes(current.mins_remaining)}
        </span>
      </div>
    );
  }

  // No upcoming HIGH-impact release. Deliberately not "no events": low and
  // medium ones hold nothing, so announcing one under this heading describes
  // a pause that is never going to happen.
  if (!next) {
    return (
      <div
        role="status"
        data-testid="news-banner"
        data-state="clear"
        className="rounded border border-line bg-surface-2 px-3 py-2 text-xs text-ink-3"
      >
        No further high-impact events on the calendar this week.
      </div>
    );
  }

  return (
    <div
      role="status"
      data-testid="news-banner"
      data-state="upcoming"
      className="flex items-center gap-3 rounded border border-line bg-surface-2 px-3 py-2"
    >
      <CalendarClock className="text-accent" size={16} />
      <div className="min-w-0">
        <p className="text-xs font-semibold text-ink-1">Next high impact</p>
        <p className="truncate text-xs text-ink-2">
          {next.title} ({next.currency})
        </p>
      </div>
      <span className="num ml-auto text-xs text-ink-3">
        {formatUtcTime(next.ts)}
      </span>
    </div>
  );
}
