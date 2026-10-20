import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { api } from "@/api/client";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { useClosedTrades } from "../hooks/useClosedTrades";
import { DayDetailDialog } from "./DayDetailDialog";
import {
  dayGains, formatGain, monthGrid, periodGain, rowDay, type DayCell, type WeekRow,
} from "./calendarGrid";

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

/**
 * A day's P&L, short enough for a cell.
 *
 * Four figures of profit in a 90-pixel square wraps and then clips, so past
 * ten thousand it becomes "+$12.4K" — the NiceGUI cell's own rule, kept
 * because the grid is the same size.
 */
function cellMoney(pnl: number): string {
  if (Math.abs(pnl) < 10_000) return formatMoney(pnl);
  return `${pnl < 0 ? "-" : "+"}$${Math.abs(pnl / 1000).toFixed(1)}K`;
}

function winRate(cell: DayCell): number {
  const wins = cell.trades.filter((t) => (Number(t.pnl) || 0) > 0).length;
  return cell.trades.length ? Math.round((wins / cell.trades.length) * 100) : 0;
}

/** A week's gain over its days in this month: the weekly total's own span. */
function weekGain(rows: Parameters<typeof periodGain>[0], balance: number | null,
                  week: WeekRow): number | null {
  const own = week.days.filter((c) => c.inMonth);
  if (!own.length) return null;
  return periodGain(rows, balance, own[0]!.date, own[own.length - 1]!.date);
}

function monthLabel(year: number, month: number): string {
  return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString("en-GB", {
    month: "long", year: "numeric", timeZone: "UTC",
  });
}

/**
 * `balance` is the broker's balance now, for each day's % gain; null (or left
 * out) when there is none, and the cells then show money only.
 */
export function CalendarSection({ days, balance = null }: {
  days: number; balance?: number | null;
}) {
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
  const newest = rows.length ? rowDay(rows[0]!) : today;
  const shown = cursor ?? (newest
    ? { year: Number(newest.slice(0, 4)), month: Number(newest.slice(5, 7)) }
    : null);

  const grid = useMemo(
    () => (shown ? monthGrid(rows, shown.year, shown.month) : null),
    [rows, shown],
  );

  const gains = useMemo(() => dayGains(rows, balance), [rows, balance]);
  const monthGain = useMemo(() => {
    if (!shown) return null;
    const mm = String(shown.month).padStart(2, "0");
    return periodGain(rows, balance, `${shown.year}-${mm}-01`, `${shown.year}-${mm}-31`);
  }, [rows, balance, shown]);

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
    <div className="space-y-3">
      <div className="flex items-center gap-1">
        <button type="button" aria-label="Previous month" onClick={() => step(-1)}
          className="rounded-md border border-line p-1.5 text-ink-3
                     transition-colors hover:bg-surface-2 hover:text-ink-1">
          <ChevronLeft size={14} />
        </button>
        <h3 data-testid="calendar-month"
          className="min-w-40 px-2 text-center text-sm font-semibold text-ink-1">
          {monthLabel(shown.year, shown.month)}
        </h3>
        <button type="button" aria-label="Next month" onClick={() => step(1)}
          className="rounded-md border border-line p-1.5 text-ink-3
                     transition-colors hover:bg-surface-2 hover:text-ink-1">
          <ChevronRight size={14} />
        </button>
        <span data-testid="calendar-total"
          className="num ml-auto rounded-md border border-line bg-surface-2/50
                     px-2.5 py-1 text-[11px] text-ink-3">
          {grid.trades} {grid.trades === 1 ? "trade" : "trades"}
          {" · "}
          <span className={cn("font-semibold", pnlColour(grid.pnl))}>
            {formatMoney(grid.pnl)}
          </span>
          {monthGain != null && (
            <span data-testid="month-gain"
              className={cn("ml-1 font-semibold", pnlColour(monthGain))}>
              ({formatGain(monthGain)})
            </span>
          )}
        </span>
      </div>

      {/* Eight columns: the trading week, then its running total. The cells
          are tall enough for four lines — day, win rate, money, count — which
          is what the NiceGUI grid showed and what the owner asked to have
          back on 2026-09-22. */}
      <div className="grid grid-cols-8 gap-1.5 text-[11px]">
        {WEEKDAYS.map((d) => (
          <div key={d}
            className="pb-1 text-center text-[10px] font-semibold uppercase
                       tracking-wider text-ink-3">
            {d}
          </div>
        ))}
        <div className="pb-1 text-center text-[10px] font-semibold uppercase
                        tracking-wider text-ink-3">
          Week
        </div>

        {grid.weeks.map((week, wi) => {
          const wGain = weekGain(rows, balance, week);
          return (
          <div key={wi} className="contents">
            {week.days.map((cell) => {
              const traded = cell.trades.length > 0;
              return (
                <button
                  key={cell.date}
                  type="button"
                  data-testid={`day-${cell.date}`}
                  data-in-month={cell.inMonth}
                  disabled={!traded}
                  onClick={() => setOpen(cell)}
                  className={cn(
                    "flex min-h-[5.5rem] flex-col rounded-lg border p-2 text-left",
                    "transition-all",
                    cell.inMonth
                      ? "border-line bg-surface-1"
                      : "border-transparent bg-transparent opacity-30",
                    // A day's colour is its result. The border carries it too,
                    // because a 10%-opacity wash alone is not legible on the
                    // light theme.
                    traded && cell.pnl > 0 && "border-profit/40 bg-profit/10",
                    traded && cell.pnl < 0 && "border-loss/40 bg-loss/10",
                    traded && "cursor-pointer hover:brightness-110 hover:shadow-sm",
                    cell.date === today && "ring-2 ring-accent",
                  )}
                >
                  <div className="flex items-baseline gap-1">
                    <span className={cn("num text-[11px] font-semibold",
                      cell.date === today ? "text-accent" : "text-ink-2")}>
                      {cell.day}
                    </span>
                    {traded && (
                      <span className="num ml-auto text-[10px] text-ink-3">
                        {winRate(cell)}%
                      </span>
                    )}
                  </div>
                  {traded && (
                    <>
                      <span className={cn("num mt-auto block text-[13px] font-bold",
                        pnlColour(cell.pnl))}>
                        {cellMoney(cell.pnl)}
                      </span>
                      <div className="flex items-baseline gap-1">
                        <span className="num text-[10px] text-ink-3">
                          {cell.trades.length}
                          {cell.trades.length === 1 ? " trade" : " trades"}
                        </span>
                        {gains.has(cell.date) && (
                          <span data-testid={`day-gain-${cell.date}`}
                            className={cn("num ml-auto text-[10px] font-semibold",
                              pnlColour(gains.get(cell.date)))}>
                            {formatGain(gains.get(cell.date)!)}
                          </span>
                        )}
                      </div>
                    </>
                  )}
                </button>
              );
            })}
            <div data-testid={`week-total-${wi}`}
              className="flex min-h-[5.5rem] flex-col justify-end rounded-lg
                         border border-line bg-surface-2/60 p-2">
              <span className={cn("num block text-[13px] font-bold",
                pnlColour(week.pnl))}>
                {week.trades ? cellMoney(week.pnl) : "—"}
              </span>
              <div className="flex items-baseline gap-1">
                <span className="num text-[10px] text-ink-3">
                  {week.trades
                    ? `${week.trades} ${week.trades === 1 ? "trade" : "trades"}`
                    : "quiet"}
                </span>
                {wGain != null && (
                  <span data-testid={`week-gain-${wi}`}
                    className={cn("num ml-auto text-[10px] font-semibold",
                      pnlColour(wGain))}>
                    {formatGain(wGain)}
                  </span>
                )}
              </div>
            </div>
          </div>
          );
        })}
      </div>

      <DayDetailDialog day={open} gain={open ? gains.get(open.date) ?? null : null}
        onClose={() => setOpen(null)} />
    </div>
  );
}
