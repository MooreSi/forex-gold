import { useMemo, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { useClosedTrades } from "../hooks/useClosedTrades";
import { formatGain, monthGrid, periodGain } from "./calendarGrid";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/**
 * A year at a glance (docs/todo/023, #5): twelve small months, each day
 * shaded by its result relative to the year's biggest day, and a strip of
 * monthly returns. Clicking a month opens it in the month view.
 *
 * **Its own 365-day read**, whatever window the Analysis tab is on: a year
 * drawn from a 30-day window is eleven empty months that look like a quiet
 * year. Mounted only while the year view is shown, so the month view never
 * pays for it. Older than 365 days is outside the read and shows as quiet.
 */
export function CalendarYearSection({ initialYear, balance, onPick }: {
  initialYear: number; balance: number | null;
  onPick: (year: number, month: number) => void;
}) {
  const poll = useClosedTrades(365);
  const [year, setYear] = useState(initialYear);
  const rows = poll.data?.rows ?? [];
  const months = useMemo(
    () => MONTHS.map((_, i) => monthGrid(rows, year, i + 1)), [rows, year]);
  const biggest = Math.max(1, ...months.flatMap((m) =>
    m.weeks.flatMap((w) => w.days.filter((d) => d.inMonth).map((d) => Math.abs(d.pnl)))));

  if (!poll.data) {
    return <EmptyState title={poll.error ? "Could not load the year" : "Loading"}
      hint={poll.error?.message} />;
  }
  if (poll.data.error) return <EmptyState title="No broker data" hint={poll.data.error} />;

  const total = months.reduce((s, m) => s + m.pnl, 0);
  const yearGain = periodGain(rows, balance, `${year}-01-01`, `${year}-12-31`);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-1">
        <button type="button" aria-label="Previous year" onClick={() => setYear(year - 1)}
          className="rounded-md border border-line p-1.5 text-ink-3 hover:bg-surface-2 hover:text-ink-1">
          <ChevronLeft size={14} />
        </button>
        <h3 data-testid="calendar-year" className="min-w-20 px-2 text-center text-sm font-semibold text-ink-1">
          {year}
        </h3>
        <button type="button" aria-label="Next year" onClick={() => setYear(year + 1)}
          className="rounded-md border border-line p-1.5 text-ink-3 hover:bg-surface-2 hover:text-ink-1">
          <ChevronRight size={14} />
        </button>
        <span data-testid="year-total" className="num ml-auto text-[11px] text-ink-3">
          <span className={cn("font-semibold", pnlColour(total))}>{formatMoney(total)}</span>
          {yearGain != null && (
            <span className={cn("ml-1 font-semibold", pnlColour(yearGain))}>({formatGain(yearGain)})</span>
          )}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {months.map((m, i) => {
          const mm = String(i + 1).padStart(2, "0");
          const gain = periodGain(rows, balance, `${year}-${mm}-01`, `${year}-${mm}-31`);
          return (
            <button key={i} type="button" data-testid={`year-month-${i + 1}`}
              onClick={() => onPick(year, i + 1)}
              className="rounded-lg border border-line bg-surface-1 p-2 text-left hover:bg-surface-2">
              <div className="mb-1 flex items-baseline gap-1 text-[11px]">
                <span className="font-semibold text-ink-2">{MONTHS[i]}</span>
                <span className={cn("num ml-auto font-semibold", m.trades ? pnlColour(m.pnl) : "text-ink-3")}>
                  {m.trades ? formatMoney(m.pnl) : "—"}
                </span>
              </div>
              <div className="grid grid-cols-7 gap-0.5">
                {m.weeks.flatMap((w) => w.days).map((d) => (
                  <span key={d.date}
                    className={cn("aspect-square rounded-[2px]",
                      !d.inMonth ? "bg-transparent"
                        : !d.trades.length ? "bg-surface-2"
                        : d.pnl >= 0 ? "bg-profit" : "bg-loss")}
                    style={d.inMonth && d.trades.length
                      ? { opacity: 0.25 + 0.75 * Math.min(1, Math.abs(d.pnl) / biggest) }
                      : undefined} />
                ))}
              </div>
              <div data-testid={`year-month-gain-${i + 1}`}
                className={cn("num mt-1 text-[10px]", gain != null ? pnlColour(gain) : "text-ink-3")}>
                {gain != null ? formatGain(gain) : m.trades ? "" : "quiet"}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
