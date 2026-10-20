import { formatMoney } from "@/components/shared/format";
import type { TradeRow } from "../hooks/useClosedTrades";
import { formatGain, type DayCell } from "./calendarGrid";

/**
 * The Calendar's statistics beyond money and win rate (docs/todo/023): what a
 * trading journal shows on its calendar. All of it from the closed-trade rows
 * the calendar already holds, so none of it can disagree with the table.
 */

const pnlOf = (t: TradeRow) => Number(t.pnl) || 0;

export interface DayStats {
  /** Gross profit over gross loss. Null with no losers: "Infinity" is not a
   *  figure anyone can compare, and a one-trade winning day is not one. */
  profitFactor: number | null;
  avgWin: number | null;
  /** Negative. */
  avgLoss: number | null;
  best: number | null;
  worst: number | null;
  fees: number;
}

export function dayStats(trades: TradeRow[]): DayStats {
  const pnls = trades.map(pnlOf);
  const wins = pnls.filter((p) => p > 0);
  const losses = pnls.filter((p) => p < 0);
  const sum = (xs: number[]) => xs.reduce((s, x) => s + x, 0);
  const grossLoss = -sum(losses);
  return {
    profitFactor: grossLoss > 0 ? sum(wins) / grossLoss : null,
    avgWin: wins.length ? sum(wins) / wins.length : null,
    avgLoss: losses.length ? sum(losses) / losses.length : null,
    best: pnls.length ? Math.max(...pnls) : null,
    worst: pnls.length ? Math.min(...pnls) : null,
    fees: sum(trades.map((t) => Number(t.fees) || 0)),
  };
}

export interface IntradayCurve {
  /** Starts at {ts: first close, cum: 0}; one point per close after it. */
  points: { ts: number; cum: number }[];
  peak: number;
  low: number;
  /** Largest fall from a running peak (the start counts as a peak of 0). */
  maxDrawdown: number;
}

/** The day's realised P&L, close by close. Realised only: an open trade's
 *  float is not in the closed-trade rows. */
export function intradayCurve(trades: TradeRow[]): IntradayCurve {
  const sorted = [...trades].sort((a, b) => (a.close_ts || 0) - (b.close_ts || 0));
  const points = [{ ts: sorted[0]?.close_ts ?? 0, cum: 0 }];
  let cum = 0, peak = 0, low = 0, maxDrawdown = 0;
  for (const t of sorted) {
    cum += pnlOf(t);
    points.push({ ts: t.close_ts, cum });
    peak = Math.max(peak, cum);
    low = Math.min(low, cum);
    maxDrawdown = Math.max(maxDrawdown, peak - cum);
  }
  return { points, peak, low, maxDrawdown };
}

export interface Streaks {
  current: { kind: "green" | "red"; length: number } | null;
  longestGreen: number;
  longestRed: number;
  greenDays: number;
  redDays: number;
}

/**
 * Runs of winning and losing days across the month shown. A day with no
 * trades neither breaks nor extends a run (a weekend is not a red day), and a
 * flat day (net exactly 0) breaks both. Padding days from the next and
 * previous months are not counted: they belong to those months' runs.
 */
export function streaks(cells: DayCell[]): Streaks {
  const days = cells.filter((c) => c.inMonth && c.trades.length)
    .sort((a, b) => a.date.localeCompare(b.date));
  let run: Streaks["current"] = null;
  const out: Streaks = { current: null, longestGreen: 0, longestRed: 0, greenDays: 0, redDays: 0 };
  for (const d of days) {
    const kind = d.pnl > 0 ? "green" : d.pnl < 0 ? "red" : null;
    if (kind === "green") out.greenDays++;
    if (kind === "red") out.redDays++;
    run = kind === null ? null
      : run && run.kind === kind ? { kind, length: run.length + 1 } : { kind, length: 1 };
    if (run?.kind === "green") out.longestGreen = Math.max(out.longestGreen, run.length);
    if (run?.kind === "red") out.longestRed = Math.max(out.longestRed, run.length);
  }
  out.current = run;
  return out;
}

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export interface WeekdayTotal { label: string; pnl: number; n: number; wins: number }

/** The month's in-month days totalled by weekday, Monday first. The weekday
 *  is the trading-clock date's, so it agrees with the column it sits in. */
export function weekdayTotals(cells: DayCell[]): WeekdayTotal[] {
  const out = WEEKDAY_LABELS.map((label) => ({ label, pnl: 0, n: 0, wins: 0 }));
  for (const c of cells) {
    if (!c.inMonth) continue;
    const [y, m, d] = c.date.split("-").map(Number);
    const slot = out[(new Date(Date.UTC(y!, m! - 1, d!)).getUTCDay() + 6) % 7]!;
    for (const t of c.trades) {
      const p = pnlOf(t);
      slot.pnl += p;
      slot.n += 1;
      if (p > 0) slot.wins += 1;
    }
  }
  return out;
}

export type CellMetric = "usd" | "pct" | "pips" | "r";

export const CELL_METRICS: readonly { key: CellMetric; label: string }[] = [
  { key: "usd", label: "$" },
  { key: "pct", label: "%" },
  { key: "pips", label: "Pips" },
  { key: "r", label: "R" },
];

/**
 * The figure a cell shows for the chosen metric. `gain` is the day's % from
 * `dayGains`, passed in because it needs the whole window, not the day. Pips
 * and R sum only the trades that carry one; a day where none does gives null
 * rather than a 0 that reads as a flat day.
 */
export function cellMetric(cell: DayCell, metric: CellMetric, gain: number | null): number | null {
  if (metric === "usd") return cell.pnl;
  if (metric === "pct") return gain;
  const key = metric === "pips" ? "pips" : "rr";
  const have = cell.trades.map((t) => t[key]).filter(
    (v): v is number => typeof v === "number" && Number.isFinite(v));
  return have.length ? have.reduce((s, v) => s + v, 0) : null;
}

/** The risk settings a goal mark reads. All optional: absent is "off". */
export interface GoalSettings {
  daily_goal_enabled?: unknown;
  daily_goal_mode?: unknown;
  daily_goal_value?: unknown;
  max_daily_loss_pct?: unknown;
}

const n = (v: unknown) => (typeof v === "number" ? v : Number(v)) || 0;

/**
 * Whether the day's running realised P&L touched the daily goal, or the daily
 * loss limit. DERIVED, not recorded: there is no record of when the governor
 * halted, so this applies TODAY's settings to the day's closes (provisional,
 * docs/simon-handover/055). Touching counts, as the goal does: a day that
 * reached +60 against a 50 goal and closed at +30 reached its goal.
 *
 * The percentages are of `dayOpen`, the day's opening balance, as
 * `risk/daily_goal.py` and the governor's loss limit measure them. Without
 * one a percentage goal or limit cannot be placed, so it is not marked.
 */
export function goalMarks(
  trades: TradeRow[], dayOpen: number | null, rs: GoalSettings,
): { goal: boolean; loss: boolean } {
  const curve = intradayCurve(trades);
  let goal = false;
  if (n(rs.daily_goal_enabled) && n(rs.daily_goal_value) > 0) {
    const target = rs.daily_goal_mode === "usd" ? n(rs.daily_goal_value)
      : dayOpen && dayOpen > 0 ? dayOpen * n(rs.daily_goal_value) / 100 : null;
    goal = target !== null && curve.peak >= target;
  }
  const limitPct = n(rs.max_daily_loss_pct);
  const loss = limitPct > 0 && !!dayOpen && dayOpen > 0
    && curve.low <= -(dayOpen * limitPct / 100);
  return { goal, loss };
}

/**
 * A cell's figure, short enough for a 90-pixel square. Money past ten
 * thousand becomes "+$12.4K", the NiceGUI cell's own rule, kept because the
 * grid is the same size. Null is "—": no figure, not a zero.
 */
export function formatMetric(metric: CellMetric, value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  if (metric === "usd") {
    if (Math.abs(value) < 10_000) return formatMoney(value);
    return `${sign}$${Math.abs(value / 1000).toFixed(1)}K`;
  }
  if (metric === "pct") return formatGain(value);
  if (metric === "pips") return `${sign}${Math.abs(value).toFixed(1)}p`;
  return `${sign}${Math.abs(value).toFixed(2)}R`;
}
