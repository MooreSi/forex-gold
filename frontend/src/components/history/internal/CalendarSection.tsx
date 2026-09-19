import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { api } from "@/api/client";
import { DialogShell } from "@/components/shared/DialogShell";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatBrokerTime, formatMoney, pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { useClosedTrades } from "../hooks/useClosedTrades";
import { bySource, monthGrid, tradingDate, type DayCell } from "./calendarGrid";

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
 */
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function monthLabel(year: number, month: number): string {
  return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString("en-GB", {
    month: "long", year: "numeric", timeZone: "UTC",
  });
}

export function CalendarSection({ days }: { days: number }) {
  const poll = useClosedTrades(days);
  const [today, setToday] = useState<string | null>(null);
  const [open, setOpen] = useState<DayCell | null>(null);
  const [cursor, setCursor] = useState<{ year: number; month: number } | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api.get<{ date: string }>("/api/history/today")
      .then((r) => !cancelled && setToday(r?.date ?? null))
      // A calendar with no "today" is still a calendar. Failing to read the
      // trading clock must not blank the month.
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  const rows = poll.data?.rows ?? [];

  // Opens on the month of the newest close rather than on the machine's
  // month: with a 7-day window in early January, "this month" can be empty
  // while every trade in the window sits in December.
  const newest = rows.length ? tradingDate(rows[0]!.close_ts) : today;
  const shown = cursor ?? (newest
    ? { year: Number(newest.slice(0, 4)), month: Number(newest.slice(5, 7)) }
    : null);

  const grid = useMemo(
    () => (shown ? monthGrid(rows, shown.year, shown.month) : null),
    [rows, shown],
  );

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

  const step = (by: number) => {
    const m = shown.month + by;
    setCursor({
      year: shown.year + (m < 1 ? -1 : m > 12 ? 1 : 0),
      month: m < 1 ? 12 : m > 12 ? 1 : m,
    });
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <button type="button" aria-label="Previous month" onClick={() => step(-1)}
          className="rounded p-1 text-ink-3 hover:bg-surface-2 hover:text-ink-1">
          <ChevronLeft size={14} />
        </button>
        <h3 data-testid="calendar-month" className="text-xs font-semibold text-ink-1">
          {monthLabel(shown.year, shown.month)}
        </h3>
        <button type="button" aria-label="Next month" onClick={() => step(1)}
          className="rounded p-1 text-ink-3 hover:bg-surface-2 hover:text-ink-1">
          <ChevronRight size={14} />
        </button>
        <span data-testid="calendar-total" className="num ml-auto text-[11px] text-ink-3">
          {grid.trades} {grid.trades === 1 ? "trade" : "trades"}
          {" · "}
          <span className={cn("font-semibold", pnlColour(grid.pnl))}>
            {formatMoney(grid.pnl)}
          </span>
        </span>
      </div>

      <div className="grid grid-cols-8 gap-1 text-[10px]">
        {WEEKDAYS.map((d) => (
          <div key={d} className="px-1 py-0.5 text-center text-ink-3">{d}</div>
        ))}
        <div className="px-1 py-0.5 text-center text-ink-3">Week</div>

        {grid.weeks.map((week, wi) => (
          <div key={wi} className="contents">
            {week.days.map((cell) => (
              <button
                key={cell.date}
                type="button"
                data-testid={`day-${cell.date}`}
                data-in-month={cell.inMonth}
                disabled={cell.trades.length === 0}
                onClick={() => setOpen(cell)}
                className={cn(
                  "min-h-12 rounded border p-1 text-left transition-colors",
                  cell.inMonth ? "border-line" : "border-transparent opacity-35",
                  cell.trades.length > 0 && "hover:bg-surface-2",
                  cell.date === today && "ring-1 ring-accent",
                  cell.pnl > 0 && "bg-profit/10",
                  cell.pnl < 0 && "bg-loss/10",
                )}
              >
                <span className="num block text-ink-3">{cell.day}</span>
                {cell.trades.length > 0 && (
                  <>
                    <span className={cn("num block font-semibold", pnlColour(cell.pnl))}>
                      {formatMoney(cell.pnl)}
                    </span>
                    <span className="num block text-[9px] text-ink-3">
                      {cell.trades.length}
                    </span>
                  </>
                )}
              </button>
            ))}
            <div data-testid={`week-total-${wi}`}
              className="min-h-12 rounded border border-line bg-surface-2 p-1">
              <span className={cn("num block font-semibold", pnlColour(week.pnl))}>
                {week.trades ? formatMoney(week.pnl) : "—"}
              </span>
              {week.trades > 0 && (
                <span className="num block text-[9px] text-ink-3">{week.trades}</span>
              )}
            </div>
          </div>
        ))}
      </div>

      <DialogShell
        open={open !== null}
        onOpenChange={(v) => !v && setOpen(null)}
        title={open ? `${open.date} — ${formatMoney(open.pnl)}` : ""}
      >
        {open && (
          <div className="space-y-3 text-xs">
            <div>
              <p className="mb-1 text-[10px] uppercase tracking-wider text-ink-3">
                By signal source
              </p>
              <ul className="space-y-0.5">
                {bySource(open.trades).map((s) => (
                  <li key={s.source} className="flex gap-2">
                    <span className="text-ink-2">{s.source}</span>
                    <span className="num ml-auto text-ink-3">{s.n}</span>
                    <span className={cn("num w-20 text-right", pnlColour(s.pnl))}>
                      {formatMoney(s.pnl)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            <div>
              <p className="mb-1 text-[10px] uppercase tracking-wider text-ink-3">
                Trades
              </p>
              <ul className="max-h-60 space-y-0.5 overflow-auto">
                {open.trades.map((t) => (
                  <li key={t.ticket} className="flex gap-2">
                    <span className="num text-ink-3">{formatBrokerTime(t.close_ts)}</span>
                    <span className={t.direction === "BUY" ? "text-profit" : "text-loss"}>
                      {t.direction}
                    </span>
                    <span className="text-ink-3">{t.reason || "—"}</span>
                    <span className={cn("num ml-auto", pnlColour(t.pnl))}>
                      {formatMoney(t.pnl)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </DialogShell>
    </div>
  );
}
