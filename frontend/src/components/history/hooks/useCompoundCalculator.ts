import { useCallback, useEffect, useMemo, useState } from "react";
import {
  breakEven, project, rateEquivalents, type CompoundInputs,
} from "../internal/compoundMath";

/**
 * The Compound Calculator's state: eight boxes, and what they project to.
 *
 * The boxes hold STRINGS, not numbers. A number state turns a half-typed
 * "0." into 0 and a cleared box into 0, and the owner watches the box they
 * are typing into rewrite itself. The string is parsed on every render and a
 * box that does not parse is reported, not coerced.
 *
 * Everything but the capital is remembered between visits, in this browser
 * only: the goal is a plan the owner comes back to, while the capital should
 * follow the account, which moves.
 */
export type FieldName = keyof CompoundInputs;

const STORAGE_KEY = "analysis.compound-calculator";
const REMEMBERED: FieldName[] = [
  "dailyPct", "daysPerWeek", "months", "reinvestPct", "monthlyDeposit",
  "drawdownPct", "losingDays",
];
const DEFAULTS: Record<FieldName, string> = {
  capital: "10000", dailyPct: "1", daysPerWeek: "5", months: "12",
  reinvestPct: "100", monthlyDeposit: "0", drawdownPct: "2", losingDays: "0",
};

function load(): Partial<Record<FieldName, string>> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== "object") return {};
    const out: Partial<Record<FieldName, string>> = {};
    for (const k of REMEMBERED) {
      const v = (parsed as Record<string, unknown>)[k];
      if (typeof v === "string") out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

function balanceText(balance: number | null): string {
  return balance != null && Number.isFinite(balance) && balance > 0
    ? String(Math.round(balance * 100) / 100)
    : DEFAULTS.capital;
}

/** The first box that is wrong, in the words the owner would use. */
function problem(i: CompoundInputs): string | null {
  if (!Number.isFinite(i.capital) || i.capital <= 0) return "Enter a starting capital above zero.";
  if (!Number.isFinite(i.dailyPct) || i.dailyPct <= 0) return "Enter a daily goal above 0%.";
  if (!Number.isInteger(i.daysPerWeek) || i.daysPerWeek < 1 || i.daysPerWeek > 7) {
    return "Trading days per week is a whole number from 1 to 7.";
  }
  if (!Number.isInteger(i.months) || i.months < 1 || i.months > 120) {
    return "The horizon is a whole number of months from 1 to 120.";
  }
  if (!Number.isFinite(i.reinvestPct) || i.reinvestPct < 0 || i.reinvestPct > 100) {
    return "Profit reinvested is a percentage from 0 to 100.";
  }
  if (!Number.isFinite(i.monthlyDeposit) || i.monthlyDeposit < 0) {
    return "The monthly deposit cannot be negative.";
  }
  if (!Number.isFinite(i.drawdownPct) || i.drawdownPct < 0 || i.drawdownPct >= 100) {
    return "The max daily drawdown is a percentage from 0 to below 100.";
  }
  if (!Number.isInteger(i.losingDays) || i.losingDays < 0 || i.losingDays > i.daysPerWeek) {
    return "Losing days per week is a whole number from 0 to the trading days per week.";
  }
  return null;
}

/** A blank box is not zero; `Number("")` says it is. */
const parse = (s: string) => (s.trim() === "" ? NaN : Number(s));

export function useCompoundCalculator(balance: number | null) {
  const [fields, setFields] = useState<Record<FieldName, string>>(
    () => ({ ...DEFAULTS, ...load(), capital: balanceText(balance) }));
  const [logScale, setLogScale] = useState(false);
  const [view, setView] = useState<"months" | "weeks">("months");

  useEffect(() => {
    try {
      const keep = Object.fromEntries(REMEMBERED.map((k) => [k, fields[k]]));
      localStorage.setItem(STORAGE_KEY, JSON.stringify(keep));
    } catch {
      // A remembered goal is a convenience. Losing it is not worth an error.
    }
  }, [fields]);

  const set = useCallback((name: FieldName, value: string) => {
    setFields((f) => ({ ...f, [name]: value }));
  }, []);

  const inputs = useMemo<CompoundInputs>(() => ({
    capital: parse(fields.capital),
    dailyPct: parse(fields.dailyPct),
    daysPerWeek: parse(fields.daysPerWeek),
    months: parse(fields.months),
    reinvestPct: parse(fields.reinvestPct),
    monthlyDeposit: parse(fields.monthlyDeposit),
    drawdownPct: parse(fields.drawdownPct),
    losingDays: parse(fields.losingDays),
  }), [fields]);

  const projection = useMemo(() => project(inputs), [inputs]);
  // The same months at half the goal, and the same losing days: the reality
  // check drawn beside it.
  const half = useMemo(
    () => project({ ...inputs, dailyPct: inputs.dailyPct / 2 }), [inputs]);
  const rates = useMemo(
    () => (projection
      ? rateEquivalents(inputs.dailyPct, inputs.daysPerWeek, inputs.drawdownPct, inputs.losingDays)
      : null),
    [projection, inputs]);
  const even = useMemo(
    () => (projection && inputs.losingDays > 0
      ? breakEven(inputs.dailyPct, inputs.daysPerWeek, inputs.drawdownPct, inputs.losingDays)
      : null),
    [projection, inputs]);

  return {
    fields,
    set,
    inputs,
    error: problem(inputs),
    projection,
    half,
    rates,
    even,
    logScale,
    setLogScale,
    view,
    setView,
    canUseBalance: balance != null && Number.isFinite(balance) && balance > 0,
    resetToBalance: () => set("capital", balanceText(balance)),
  };
}

export type CompoundCalculator = ReturnType<typeof useCompoundCalculator>;
