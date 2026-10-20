import { formatMoney, pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import type { TradeRow } from "../hooks/useClosedTrades";
import { bySession, bySource, type Tally } from "./calendarGrid";

/**
 * A day split two ways: by market session and by signal source.
 *
 * Two breakdowns, not one. The React port carried over only "by signal
 * source"; the NiceGUI page also split the day by market session, and on
 * 2026-09-22 the owner asked for it back — "when clicking into each day it
 * should also list the profitability per market as well". One instrument is
 * traded here, so the market that varies is which one was OPEN.
 *
 * Every row carries its wins as "3 / 5" beside the money. Ten dollars from one
 * trade in four and ten dollars from four in four are not the same day, and a
 * bare percentage on a two-trade row says almost nothing.
 */
function BreakdownRow({ row, label, testId }: {
  row: Tally; label: string; testId: string;
}) {
  const winPct = row.n ? Math.round((row.wins / row.n) * 100) : 0;
  return (
    <li
      data-testid={testId}
      className="flex items-center gap-2 rounded px-1.5 py-1 odd:bg-surface-2/40"
    >
      <span className="truncate text-ink-2">{label}</span>
      <span className="num ml-auto w-12 shrink-0 text-right text-ink-3">
        {row.wins} / {row.n}
      </span>
      <span className="num w-10 shrink-0 text-right text-ink-3">{winPct}%</span>
      <span className={cn("num w-20 shrink-0 text-right font-semibold",
        pnlColour(row.pnl))}>
        {formatMoney(row.pnl)}
      </span>
    </li>
  );
}

function Heading({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider
                    text-ink-3">
      <span>{children}</span>
      <span className="ml-auto w-12 text-right">Wins</span>
      <span className="w-10 text-right">Win%</span>
      <span className="w-20 text-right">Net</span>
    </div>
  );
}

export function DayBreakdownSection({ trades }: { trades: TradeRow[] }) {
  const sessions = bySession(trades);
  return (
    <>
      <section className="space-y-1">
        <Heading>By market session</Heading>
        <ul className="space-y-0.5">
          {sessions.rows.map((s) => (
            <BreakdownRow key={s.key} row={s} label={s.label}
              testId={`session-${s.key}`} />
          ))}
        </ul>
        {sessions.unplaced > 0 && (
          <p className="text-[10px] text-ink-3">
            {sessions.unplaced}{" "}
            {sessions.unplaced === 1 ? "trade had" : "trades had"} no close
            time and {sessions.unplaced === 1 ? "is" : "are"} not shown
            here.
          </p>
        )}
      </section>

      <section className="space-y-1">
        <Heading>By signal source</Heading>
        <ul className="space-y-0.5">
          {bySource(trades).map((s) => (
            <BreakdownRow key={s.source} row={s} label={s.source}
              testId={`source-${s.source}`} />
          ))}
        </ul>
      </section>
    </>
  );
}
