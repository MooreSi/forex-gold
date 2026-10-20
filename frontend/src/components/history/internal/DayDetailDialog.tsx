import { DialogShell } from "@/components/shared/DialogShell";
import { formatBrokerTime, formatMoney, formatUtcTime, pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import type { CalendarNews, JournalDraft, JournalEntry } from "../hooks/useCalendarController";
import { DayBreakdownSection } from "./DayBreakdownSection";
import { DayJournalForm } from "./DayJournalForm";
import { DayStatsSection } from "./DayStatsSection";
import { formatGain, type DayCell } from "./calendarGrid";
import type { GoalSettings } from "./calendarStats";

/**
 * One day, opened from the calendar: its numbers, its realised curve, the
 * session and source breakdowns, the news that was scheduled, every trade
 * with its strategy, and the day's journal (docs/todo/023).
 */
export function DayDetailDialog({
  day, gain = null, dayOpen = null, risk = {}, news = [], journal = null,
  onSaveJournal, onClose,
}: {
  day: DayCell | null;
  /** The day's % of its opening balance; null when it cannot be measured. */
  gain?: number | null;
  dayOpen?: number | null;
  risk?: GoalSettings;
  news?: CalendarNews[];
  journal?: JournalEntry | null;
  onSaveJournal?: (day: string, draft: JournalDraft) => Promise<void>;
  onClose: () => void;
}) {
  const wins = day ? day.trades.filter((t) => (Number(t.pnl) || 0) > 0).length : 0;

  return (
    <DialogShell
      open={day !== null}
      onOpenChange={(v) => !v && onClose()}
      title={day ? `${day.date} — ${formatMoney(day.pnl)}` : ""}
    >
      {day && (
        <div className="space-y-4 text-xs">
          <p className="text-[11px] text-ink-3">
            {day.trades.length} {day.trades.length === 1 ? "trade" : "trades"}
            {" · "}
            {day.trades.length
              ? `${Math.round((wins / day.trades.length) * 100)}% win rate`
              : "no trades"}
            {gain != null && (
              <>
                {" · "}
                <span data-testid="day-detail-gain"
                  className={cn("num font-semibold", pnlColour(gain))}>
                  {formatGain(gain)}
                </span>
                {" of the day's opening balance"}
              </>
            )}
          </p>

          <DayStatsSection trades={day.trades} dayOpen={dayOpen} risk={risk} />
          <DayBreakdownSection trades={day.trades} />

          {news.length > 0 && (
            <section className="space-y-1" data-testid="day-news">
              <p className="text-[10px] uppercase tracking-wider text-ink-3">News (UTC)</p>
              <ul className="space-y-0.5">
                {news.map((e) => (
                  <li key={`${e.ts}-${e.currency}-${e.title}`}
                    className="flex items-center gap-2 rounded px-1.5 py-1 odd:bg-surface-2/40">
                    <span className="num text-ink-3">{formatUtcTime(e.ts)}</span>
                    <span className="text-ink-2">{e.currency}</span>
                    <span className="truncate text-ink-2">{e.title}</span>
                    <span className={cn("ml-auto shrink-0 text-[10px] uppercase",
                      e.impact === "high" ? "text-loss" : "text-ink-3")}>
                      {e.impact}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className="space-y-1">
            <p className="text-[10px] uppercase tracking-wider text-ink-3">
              Trades
            </p>
            <ul className="max-h-60 space-y-0.5 overflow-auto">
              {day.trades.map((t) => (
                <li key={t.ticket}
                  className="flex items-center gap-2 rounded px-1.5 py-1
                             odd:bg-surface-2/40">
                  <span className="num text-ink-3">
                    {formatBrokerTime(t.close_ts)}
                  </span>
                  <span className={t.direction === "BUY" ? "text-profit" : "text-loss"}>
                    {t.direction}
                  </span>
                  {/* Owner, 2026-10-20: which strategy ran each trade. */}
                  <span data-testid={`trade-strategy-${t.ticket}`}
                    className="shrink-0 rounded bg-surface-2 px-1.5 text-[10px] text-ink-2">
                    {t.strategy || "—"}
                  </span>
                  <span className="truncate text-ink-3">{t.reason || "—"}</span>
                  <span className={cn("num ml-auto shrink-0", pnlColour(t.pnl))}>
                    {formatMoney(t.pnl)}
                  </span>
                </li>
              ))}
            </ul>
          </section>

          {onSaveJournal && (
            <DayJournalForm day={day.date} entry={journal} onSave={onSaveJournal} />
          )}
        </div>
      )}
    </DialogShell>
  );
}
