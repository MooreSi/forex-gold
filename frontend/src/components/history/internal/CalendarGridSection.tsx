import { NotebookPen, Newspaper, ShieldAlert, Target } from "lucide-react";
import { pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import type { CalendarController } from "../hooks/useCalendarController";
import { formatGain, type DayCell, type MonthGrid, type WeekRow, periodGain } from "./calendarGrid";
import { cellMetric, formatMetric, goalMarks } from "./calendarStats";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function winRate(cell: DayCell): number {
  const wins = cell.trades.filter((t) => (Number(t.pnl) || 0) > 0).length;
  return cell.trades.length ? Math.round((wins / cell.trades.length) * 100) : 0;
}

/** A week's gain over its days in this month: the weekly total's own span. */
function weekGain(c: CalendarController, week: WeekRow): number | null {
  const own = week.days.filter((d) => d.inMonth);
  if (!own.length) return null;
  return periodGain(c.rows, c.balance, own[0]!.date, own[own.length - 1]!.date);
}

/**
 * One day's square. The main figure is the chosen metric; the colour is
 * always the day's money result, so switching to pips cannot repaint a losing
 * day green. Markers along the top: goal reached, loss limit reached, news,
 * a journal note (docs/todo/023).
 */
function DayButton({ cell, c }: { cell: DayCell; c: CalendarController }) {
  const traded = cell.trades.length > 0;
  const gain = c.gains.get(cell.date) ?? null;
  const marks = traded ? goalMarks(cell.trades, c.opens.get(cell.date) ?? null, c.risk)
    : { goal: false, loss: false };
  const news = cell.inMonth ? c.newsByDay.get(cell.date) ?? [] : [];
  const note = cell.inMonth ? c.journalByDay.get(cell.date) : undefined;
  return (
    <button
      type="button"
      data-testid={`day-${cell.date}`}
      data-in-month={cell.inMonth}
      disabled={!traded}
      onClick={() => c.setOpen(cell)}
      className={cn(
        "flex min-h-[5.5rem] flex-col rounded-lg border p-2 text-left transition-all",
        cell.inMonth ? "border-line bg-surface-1" : "border-transparent bg-transparent opacity-30",
        // A day's colour is its result. The border carries it too, because a
        // 10%-opacity wash alone is not legible on the light theme.
        traded && cell.pnl > 0 && "border-profit/40 bg-profit/10",
        traded && cell.pnl < 0 && "border-loss/40 bg-loss/10",
        traded && "cursor-pointer hover:brightness-110 hover:shadow-sm",
        cell.date === c.today && "ring-2 ring-accent",
      )}
    >
      <div className="flex items-center gap-1">
        <span className={cn("num text-[11px] font-semibold",
          cell.date === c.today ? "text-accent" : "text-ink-2")}>
          {cell.day}
        </span>
        {marks.goal && (
          <Target size={10} data-testid={`goal-mark-${cell.date}`}
            className="text-profit" aria-label="Daily goal reached" />
        )}
        {marks.loss && (
          <ShieldAlert size={10} data-testid={`loss-mark-${cell.date}`}
            className="text-loss" aria-label="Daily loss limit reached" />
        )}
        {news.length > 0 && (
          <span data-testid={`news-mark-${cell.date}`}
            title={news.map((e) => `${e.currency} ${e.title}`).join("\n")}
            className={cn("flex items-center",
              news.some((e) => e.impact === "high") ? "text-loss" : "text-ink-3")}>
            <Newspaper size={10} aria-label={`${news.length} news events`} />
          </span>
        )}
        {note && (
          <NotebookPen size={10} data-testid={`note-mark-${cell.date}`}
            className="text-accent" aria-label="Journal note" />
        )}
        {traded && (
          <span className="num ml-auto text-[10px] text-ink-3">{winRate(cell)}%</span>
        )}
      </div>
      {traded && (
        <>
          <span data-testid={`day-metric-${cell.date}`}
            className={cn("num mt-auto block text-[13px] font-bold", pnlColour(cell.pnl))}>
            {formatMetric(c.metric, cellMetric(cell, c.metric, gain))}
          </span>
          <div className="flex items-baseline gap-1">
            <span className="num text-[10px] text-ink-3">
              {cell.trades.length}
              {cell.trades.length === 1 ? " trade" : " trades"}
            </span>
            {gain !== null && c.metric !== "pct" && (
              <span data-testid={`day-gain-${cell.date}`}
                className={cn("num ml-auto text-[10px] font-semibold", pnlColour(gain))}>
                {formatGain(gain)}
              </span>
            )}
          </div>
        </>
      )}
    </button>
  );
}

/**
 * The month as a grid: eight columns, the trading week then its running
 * total. Monday-first because the trading week is (see `monthGrid`).
 */
export function CalendarGridSection({ c, grid }: { c: CalendarController; grid: MonthGrid }) {
  return (
    <div className="grid grid-cols-8 gap-1.5 text-[11px]">
      {[...WEEKDAYS, "Week"].map((d) => (
        <div key={d}
          className="pb-1 text-center text-[10px] font-semibold uppercase tracking-wider text-ink-3">
          {d}
        </div>
      ))}
      {grid.weeks.map((week, wi) => {
        const wGain = weekGain(c, week);
        return (
          <div key={wi} className="contents">
            {week.days.map((cell) => <DayButton key={cell.date} cell={cell} c={c} />)}
            <div data-testid={`week-total-${wi}`}
              className="flex min-h-[5.5rem] flex-col justify-end rounded-lg border
                         border-line bg-surface-2/60 p-2">
              <span className={cn("num block text-[13px] font-bold", pnlColour(week.pnl))}>
                {week.trades ? formatMetric("usd", week.pnl) : "—"}
              </span>
              <div className="flex items-baseline gap-1">
                <span className="num text-[10px] text-ink-3">
                  {week.trades
                    ? `${week.trades} ${week.trades === 1 ? "trade" : "trades"}`
                    : "quiet"}
                </span>
                {wGain != null && (
                  <span data-testid={`week-gain-${wi}`}
                    className={cn("num ml-auto text-[10px] font-semibold", pnlColour(wGain))}>
                    {formatGain(wGain)}
                  </span>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
