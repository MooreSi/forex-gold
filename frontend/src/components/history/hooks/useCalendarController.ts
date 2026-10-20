import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { asArray, asObject } from "@/lib/asArray";
import { useClosedTrades } from "./useClosedTrades";
import {
  dayGains, dayOpenBalances, monthGrid, periodGain, rowDay, type DayCell,
} from "../internal/calendarGrid";
import type { CellMetric, GoalSettings } from "../internal/calendarStats";

/** One economic-calendar event, filed under its trading-clock day. */
export interface CalendarNews {
  day: string; ts: number; title: string; currency: string; impact: string;
}

/** The owner's note on one trading day (docs/todo/023, #8). */
export interface JournalEntry {
  day: string; note: string; tags: string[]; mood: number | null; updated_at: number;
}

export type JournalDraft = Pick<JournalEntry, "note" | "tags" | "mood">;

const METRIC_KEY = "calendar/metric";
const METRICS: CellMetric[] = ["usd", "pct", "pips", "r"];

// Per-viewer convenience, so browser storage and not a setting. Either call
// can throw (private window, blocked storage): the calendar still works.
function storedMetric(): CellMetric {
  try {
    const v = localStorage.getItem(METRIC_KEY) as CellMetric | null;
    return v && METRICS.includes(v) ? v : "usd";
  } catch {
    return "usd";
  }
}

function monthSpan(shown: { year: number; month: number }): { start: string; end: string } {
  const mm = String(shown.month).padStart(2, "0");
  // The 31st for every month: these are compared as strings against real
  // dates, and no real date of the month sorts after it.
  return { start: `${shown.year}-${mm}-01`, end: `${shown.year}-${mm}-31` };
}

/**
 * Everything the Calendar shows, and the reads behind it.
 *
 * The trades come from the poll the trade table shares. The rest is read once
 * per month shown, not polled: the risk settings (for goal marks), the news
 * history and the journal change when someone acts, not on a clock.
 */
export function useCalendarController(days: number, balance: number | null) {
  const poll = useClosedTrades(days);
  const [today, setToday] = useState<string | null>(null);
  const [cursor, setCursor] = useState<{ year: number; month: number } | null>(null);
  const [open, setOpen] = useState<DayCell | null>(null);
  const [view, setView] = useState<"month" | "year">("month");
  const [metric, setMetricState] = useState<CellMetric>(storedMetric);
  const [risk, setRisk] = useState<GoalSettings>({});
  const [news, setNews] = useState<CalendarNews[]>([]);
  const [journal, setJournal] = useState<JournalEntry[]>([]);

  useEffect(() => {
    let cancelled = false;
    void api.get<{ date: string }>("/api/history/today")
      .then((r) => !cancelled && setToday(r?.date ?? null))
      // A calendar with no "today" is still a calendar. Failing to read the
      // trading clock must not blank the month.
      .catch(() => undefined);
    // Goal marks only. A failed read marks nothing, which is the honest
    // reading of "we do not know your goal".
    void api.get<GoalSettings>("/api/settings/risk")
      .then((r) => !cancelled && setRisk(asObject(r) as GoalSettings))
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
  const span = shown ? monthSpan(shown) : null;

  const loadMonth = useCallback(async (s: { start: string; end: string }) => {
    const q = `start=${s.start}&end=${s.end}`;
    const [n, j] = await Promise.all([
      api.get<{ events?: unknown }>(`/api/calendar/news?${q}`).catch(() => null),
      api.get<{ entries?: unknown }>(`/api/calendar/journal?${q}`).catch(() => null),
    ]);
    setNews(asArray(asObject(n).events) as CalendarNews[]);
    setJournal(asArray(asObject(j).entries) as JournalEntry[]);
  }, []);

  useEffect(() => {
    if (span) void loadMonth(span);
  }, [span?.start, loadMonth]);

  const grid = useMemo(
    () => (shown ? monthGrid(rows, shown.year, shown.month) : null),
    [rows, shown?.year, shown?.month],
  );
  const gains = useMemo(() => dayGains(rows, balance), [rows, balance]);
  const opens = useMemo(() => dayOpenBalances(rows, balance), [rows, balance]);
  const monthGain = span ? periodGain(rows, balance, span.start, span.end) : null;

  const newsByDay = useMemo(() => {
    const out = new Map<string, CalendarNews[]>();
    for (const e of news) out.set(e.day, [...(out.get(e.day) ?? []), e]);
    return out;
  }, [news]);
  const journalByDay = useMemo(
    () => new Map(journal.map((e) => [e.day, e])), [journal]);

  const setMetric = (m: CellMetric) => {
    setMetricState(m);
    try { localStorage.setItem(METRIC_KEY, m); } catch { /* per-viewer only */ }
  };

  const step = (by: number) => {
    if (!shown) return;
    const m = shown.month + by;
    setCursor({
      year: shown.year + (m < 1 ? -1 : m > 12 ? 1 : 0),
      month: m < 1 ? 12 : m > 12 ? 1 : m,
    });
  };

  const pickMonth = (year: number, month: number) => {
    setCursor({ year, month });
    setView("month");
  };

  /** Saves and re-reads the month, so what is shown is what was stored. */
  const saveJournal = async (day: string, draft: JournalDraft) => {
    await api.put<JournalEntry>(`/api/calendar/journal/${day}`, draft);
    if (span) await loadMonth(span);
  };

  return {
    poll, today, shown, grid, rows, balance, gains, opens, monthGain,
    view, setView, metric, setMetric, step, pickMonth,
    open, setOpen, risk, newsByDay, journalByDay, saveJournal,
  };
}

export type CalendarController = ReturnType<typeof useCalendarController>;
