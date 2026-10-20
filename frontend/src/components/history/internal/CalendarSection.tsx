import { ChevronLeft, ChevronRight } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { useCalendarController } from "../hooks/useCalendarController";
import { CalendarGridSection } from "./CalendarGridSection";
import { CalendarSummarySection } from "./CalendarSummarySection";
import { CalendarYearSection } from "./CalendarYearSection";
import { DayDetailDialog } from "./DayDetailDialog";
import { formatGain } from "./calendarGrid";
import { CELL_METRICS } from "./calendarStats";

/**
 * A month of closed trades, day by day.
 *
 * The NiceGUI Analysis tab had six sub-tabs and the React port had four of
 * them; this is the Calendar. Built from the rows the trade table already
 * fetched, so the two cannot disagree about what a day earned, and using the
 * same poll key so opening both costs one request.
 *
 * **Today comes from the backend, not from the browser.** `/api/history/today`
 * answers on the TRADING clock, which differs from the machine's date whenever
 * a clock offset is configured — the whole point on a VPS in another timezone.
 * A calendar that highlighted the machine's today would mark the wrong day's
 * trades.
 *
 * What a trading journal's calendar shows beyond that (docs/todo/023) is in
 * the grid's markers, the summary under it, the year view and the day dialog.
 *
 * `balance` is the broker's balance now, for the % gains; null (or left out)
 * when there is none, and the calendar then shows money only.
 */
function monthLabel(year: number, month: number): string {
  return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString("en-GB", {
    month: "long", year: "numeric", timeZone: "UTC",
  });
}

const pill = (active: boolean) => cn(
  "rounded px-2 py-0.5 text-[11px] transition-colors",
  active ? "bg-surface-3 text-ink-1" : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
);

export function CalendarSection({ days, balance = null }: {
  days: number; balance?: number | null;
}) {
  const c = useCalendarController(days, balance);
  const { poll, grid, shown } = c;

  if (!poll.data) {
    return <EmptyState title={poll.error ? "Could not load the calendar" : "Loading"}
      hint={poll.error?.message} />;
  }
  if (poll.data.error) {
    return <EmptyState title="No broker data" hint={poll.data.error} />;
  }
  if (!grid || !shown) {
    return <EmptyState title="No closed trades to put on a calendar" />;
  }

  const viewToggle = (
    <div className="flex rounded-md border border-line p-0.5" role="group" aria-label="Calendar view">
      {(["month", "year"] as const).map((v) => (
        <button key={v} type="button" aria-pressed={c.view === v}
          onClick={() => c.setView(v)} className={pill(c.view === v)}>
          {v === "month" ? "Month" : "Year"}
        </button>
      ))}
    </div>
  );

  if (c.view === "year") {
    return (
      <div className="space-y-3">
        <div className="flex justify-end">{viewToggle}</div>
        <CalendarYearSection initialYear={shown.year} balance={balance} onPick={c.pickMonth} />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1">
        <button type="button" aria-label="Previous month" onClick={() => c.step(-1)}
          className="rounded-md border border-line p-1.5 text-ink-3
                     transition-colors hover:bg-surface-2 hover:text-ink-1">
          <ChevronLeft size={14} />
        </button>
        <h3 data-testid="calendar-month"
          className="min-w-40 px-2 text-center text-sm font-semibold text-ink-1">
          {monthLabel(shown.year, shown.month)}
        </h3>
        <button type="button" aria-label="Next month" onClick={() => c.step(1)}
          className="rounded-md border border-line p-1.5 text-ink-3
                     transition-colors hover:bg-surface-2 hover:text-ink-1">
          <ChevronRight size={14} />
        </button>
        <div className="ml-2 flex rounded-md border border-line p-0.5" role="group"
          aria-label="Cell figure">
          {CELL_METRICS.map((m) => (
            <button key={m.key} type="button" aria-pressed={c.metric === m.key}
              onClick={() => c.setMetric(m.key)} className={pill(c.metric === m.key)}>
              {m.label}
            </button>
          ))}
        </div>
        <div className="ml-1">{viewToggle}</div>
        <span data-testid="calendar-total"
          className="num ml-auto rounded-md border border-line bg-surface-2/50
                     px-2.5 py-1 text-[11px] text-ink-3">
          {grid.trades} {grid.trades === 1 ? "trade" : "trades"}
          {" · "}
          <span className={cn("font-semibold", pnlColour(grid.pnl))}>
            {formatMoney(grid.pnl)}
          </span>
          {c.monthGain != null && (
            <span data-testid="month-gain"
              className={cn("ml-1 font-semibold", pnlColour(c.monthGain))}>
              ({formatGain(c.monthGain)})
            </span>
          )}
        </span>
      </div>

      <CalendarGridSection c={c} grid={grid} />
      <CalendarSummarySection cells={grid.weeks.flatMap((w) => w.days)} />

      <DayDetailDialog
        day={c.open}
        gain={c.open ? c.gains.get(c.open.date) ?? null : null}
        dayOpen={c.open ? c.opens.get(c.open.date) ?? null : null}
        risk={c.risk}
        news={c.open ? c.newsByDay.get(c.open.date) ?? [] : []}
        journal={c.open ? c.journalByDay.get(c.open.date) ?? null : null}
        onSaveJournal={c.saveJournal}
        onClose={() => c.setOpen(null)}
      />
    </div>
  );
}
