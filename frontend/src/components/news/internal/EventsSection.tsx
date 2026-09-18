import { EmptyState } from "@/components/shared/EmptyState";
import { cn } from "@/lib/cn";
import type { NewsEvent } from "@/api/types";

const IMPACT_CLASS: Record<string, string> = {
  high: "text-loss",
  medium: "text-warning",
  low: "text-ink-3",
  holiday: "text-ink-3",
};

function utcTime(ts: number): string {
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  }).format(new Date(ts * 1000));
}

function utcDay(ts: number): string {
  return new Intl.DateTimeFormat("en-GB", {
    weekday: "short", day: "2-digit", month: "short", timeZone: "UTC",
  }).format(new Date(ts * 1000));
}

/**
 * The week's releases, grouped by day.
 *
 * Times are UTC and say so. The calendar publishes in UTC and the blackout
 * windows are computed in it; rendering them in London time would put an
 * event an hour from where the engine thinks it is for half the year.
 */
export function EventsSection({ events }: { events: NewsEvent[] }) {
  if (events.length === 0) {
    return (
      <EmptyState
        title="No events match the current filter"
        hint="Turn off the gold-only or upcoming-only filters to see the rest of the week."
      />
    );
  }

  const days: { day: string; rows: NewsEvent[] }[] = [];
  for (const event of events) {
    const day = utcDay(event.ts);
    const last = days[days.length - 1];
    if (last && last.day === day) last.rows.push(event);
    else days.push({ day, rows: [event] });
  }

  return (
    <div className="space-y-4">
      {days.map(({ day, rows }) => (
        <section key={day}>
          <h3 className="mb-1 flex items-baseline gap-2 border-b border-line pb-1">
            <span className="text-xs font-semibold text-ink-1">{day}</span>
            <span className="text-[10px] text-ink-3">
              {rows.length} event{rows.length === 1 ? "" : "s"}
            </span>
          </h3>
          <table className="w-full text-xs">
            <tbody>
              {rows.map((e, i) => (
                <tr key={`${e.ts}-${e.title}-${i}`} className="border-b border-line/50">
                  <td className="num w-20 py-1.5 text-ink-3">{utcTime(e.ts)} UTC</td>
                  <td className="num w-12 py-1.5 text-ink-2">{e.currency}</td>
                  <td className={cn("w-16 py-1.5 uppercase", IMPACT_CLASS[e.impact] ?? "text-ink-3")}>
                    {e.impact || "—"}
                  </td>
                  <td className="py-1.5 text-ink-1">{e.title}</td>
                  <td className="num w-40 py-1.5 text-right text-ink-3">
                    {e.forecast && `fc ${e.forecast}`}
                    {e.forecast && e.previous ? " · " : ""}
                    {e.previous && `prev ${e.previous}`}
                  </td>
                  <td
                    className="num w-10 py-1.5 text-right text-ink-3"
                    title="How much this release tends to move gold"
                  >
                    {e.score.toFixed(1)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}
