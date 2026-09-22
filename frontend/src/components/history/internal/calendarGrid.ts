import type { TradeRow } from "../hooks/useClosedTrades";

/**
 * Closed trades arranged as a month, the way the NiceGUI Calendar tab showed
 * them.
 *
 * **Which day a trade belongs to is the whole problem.** A close stamp is
 * broker time (UTC+3), so a trade that closed at 01:30 broker time on Tuesday
 * closed at 22:30 London on Monday — and putting it on Tuesday moves a loss
 * into the wrong week, wrong month and wrong weekly total. Everything here
 * goes through one shift, in one place.
 *
 * Built from the rows the trade table already fetched rather than a read of
 * its own: the calendar and the table must agree about what a day earned.
 */
const MT5_UTC_OFFSET_SECONDS = 3 * 60 * 60;

export interface DayCell {
  /** ISO date, YYYY-MM-DD, on the trading clock. */
  date: string;
  day: number;
  pnl: number;
  trades: TradeRow[];
  /** False for the leading and trailing cells that pad the grid. */
  inMonth: boolean;
}

export interface WeekRow {
  days: DayCell[];
  pnl: number;
  trades: number;
}

export interface MonthGrid {
  year: number;
  /** 1-12. */
  month: number;
  weeks: WeekRow[];
  pnl: number;
  trades: number;
}

/** The ISO date a close stamp belongs to, on the trading clock. */
export function tradingDate(closeTs: number): string {
  const d = new Date((closeTs - MT5_UTC_OFFSET_SECONDS) * 1000);
  // UTC getters after the shift: a local-time getter would apply the viewer's
  // own timezone on top and move the boundary again, differently per machine.
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function groupByDay(rows: TradeRow[]): Map<string, TradeRow[]> {
  const out = new Map<string, TradeRow[]>();
  for (const row of rows ?? []) {
    if (!row?.close_ts) continue;
    const key = tradingDate(row.close_ts);
    const bucket = out.get(key);
    if (bucket) bucket.push(row);
    else out.set(key, [row]);
  }
  return out;
}

function iso(year: number, month: number, day: number): string {
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/**
 * A Monday-first month grid, padded to whole weeks.
 *
 * Monday-first because the trading week is, and because a Sunday-first grid
 * splits the weekend across two rows — which puts Friday's close and the
 * Sunday open in different weekly totals.
 */
export function monthGrid(rows: TradeRow[], year: number, month: number): MonthGrid {
  const byDay = groupByDay(rows);
  const first = new Date(Date.UTC(year, month - 1, 1));
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();

  // getUTCDay is 0=Sunday. Monday-first means Monday is 0.
  const lead = (first.getUTCDay() + 6) % 7;

  const cells: DayCell[] = [];
  const push = (y: number, m: number, d: number, inMonth: boolean) => {
    const date = iso(y, m, d);
    const trades = byDay.get(date) ?? [];
    cells.push({
      date, day: d, inMonth, trades,
      pnl: trades.reduce((sum, t) => sum + (Number(t.pnl) || 0), 0),
    });
  };

  // Leading pad from the previous month, so the first row is a real week.
  const prevMonth = month === 1 ? 12 : month - 1;
  const prevYear = month === 1 ? year - 1 : year;
  const prevDays = new Date(Date.UTC(prevYear, prevMonth, 0)).getUTCDate();
  for (let i = lead; i > 0; i--) push(prevYear, prevMonth, prevDays - i + 1, false);

  for (let d = 1; d <= daysInMonth; d++) push(year, month, d, true);

  const nextMonth = month === 12 ? 1 : month + 1;
  const nextYear = month === 12 ? year + 1 : year;
  let trail = 1;
  while (cells.length % 7 !== 0) push(nextYear, nextMonth, trail++, false);

  const weeks: WeekRow[] = [];
  for (let i = 0; i < cells.length; i += 7) {
    const days = cells.slice(i, i + 7);
    weeks.push({
      days,
      // Weekly totals count only this month's days: a total that included the
      // padding would double-count the same trades in two months.
      pnl: days.reduce((s, c) => s + (c.inMonth ? c.pnl : 0), 0),
      trades: days.reduce((s, c) => s + (c.inMonth ? c.trades.length : 0), 0),
    });
  }

  const own = cells.filter((c) => c.inMonth);
  return {
    year, month, weeks,
    pnl: own.reduce((s, c) => s + c.pnl, 0),
    trades: own.reduce((s, c) => s + c.trades.length, 0),
  };
}

export interface Tally {
  pnl: number;
  /** Trades in this bucket. */
  n: number;
  /** Winners among them. Shown as "3 / 5": on a two-trade day a bare "50%"
   *  says almost nothing, and this panel is most useful on those days. */
  wins: number;
}

function tally(
  trades: TradeRow[],
  keyOf: (t: TradeRow) => string | null,
): (Tally & { key: string })[] {
  const acc = new Map<string, { pnl: number; n: number; wins: number }>();
  for (const t of trades ?? []) {
    const key = keyOf(t);
    if (key === null) continue;
    const cur = acc.get(key) ?? { pnl: 0, n: 0, wins: 0 };
    const pnl = Number(t.pnl) || 0;
    cur.pnl += pnl;
    cur.n += 1;
    if (pnl > 0) cur.wins += 1;
    acc.set(key, cur);
  }
  return [...acc.entries()].map(([key, v]) => ({ key, ...v }));
}

/** Per-source totals for one day, worst first. */
export function bySource(trades: TradeRow[]): (Tally & { source: string })[] {
  return tally(trades, (t) => t.source || "unattributed")
    .map(({ key, ...v }) => ({ source: key, ...v }))
    .sort((a, b) => a.pnl - b.pnl);
}

/**
 * The trading sessions, in the order a day runs through them.
 *
 * The same four the backend's `_session_for_hour` names and the hourly heat
 * map reads. Chronological rather than sorted by P&L: this table is read as a
 * day, and "the Asian session gave it all back" is a sentence about order.
 */
export const SESSIONS: readonly { key: string; label: string }[] = [
  { key: "asian", label: "Asian" },
  { key: "london", label: "London" },
  { key: "overlap", label: "Overlap (LDN+NY)" },
  { key: "ny", label: "New York" },
];

export interface SessionSplit {
  rows: (Tally & { key: string; label: string })[];
  /** Trades with no session on them — no close stamp in the broker's record.
   *  Counted, never bucketed: a wrong session reads as a real result. */
  unplaced: number;
}

/** Per-session totals for one day, in session order. */
export function bySession(trades: TradeRow[]): SessionSplit {
  const found = new Map(tally(trades, (t) => t.session || null).map((r) => [r.key, r]));
  const rows = SESSIONS
    .filter((s) => found.has(s.key))
    .map((s) => ({ ...(found.get(s.key) as Tally & { key: string }),
                   label: s.label }));
  const placed = rows.reduce((sum, r) => sum + r.n, 0);
  return { rows, unplaced: (trades ?? []).length - placed };
}
