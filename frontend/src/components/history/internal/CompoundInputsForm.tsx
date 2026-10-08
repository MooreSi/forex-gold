import { useId } from "react";
import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import type { CompoundCalculator, FieldName } from "../hooks/useCompoundCalculator";

/**
 * The calculator's eight inputs, each a slider with a box beside it.
 *
 * The slider is for feel -- drag the goal and watch the curve bend. The box is
 * for an exact figure, and it is the one that is authoritative: it can hold a
 * value past either end of the slider (a $2M account, a 0.01% goal), and the
 * slider then rests at its nearest end rather than rewriting what was typed.
 */
interface Spec {
  name: FieldName;
  label: string;
  min: number;
  max: number;
  step: number;
  unit?: string;
  prefix?: string;
  hint: string;
}

function Slider({ spec, value, onChange }: {
  spec: Spec; value: string; onChange: (v: string) => void;
}) {
  const id = useId();
  const n = Number(value);
  const pinned = Number.isFinite(n) ? Math.min(spec.max, Math.max(spec.min, n)) : spec.min;
  const fill = ((pinned - spec.min) / (spec.max - spec.min)) * 100;

  return (
    <div className="space-y-2 rounded border border-line bg-surface-2 px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="text-[10px] uppercase tracking-wide text-ink-3">
          {spec.label}
        </label>
        <div className="flex items-center gap-1 rounded border border-line bg-surface-1 px-1.5 focus-within:border-accent">
          {spec.prefix && <span className="text-[11px] text-ink-3">{spec.prefix}</span>}
          <Tooltip label={spec.hint}>
            <input
              id={id}
              type="number"
              inputMode="decimal"
              min={spec.min}
              step={spec.step}
              value={value}
              onChange={(e) => onChange(e.target.value)}
              className="num w-24 bg-transparent py-0.5 text-right text-sm font-semibold text-ink-1 outline-none"
            />
          </Tooltip>
          {spec.unit && <span className="text-[11px] text-ink-3">{spec.unit}</span>}
        </div>
      </div>
      <Tooltip label={spec.hint}>
        <input
          type="range"
          aria-label={`${spec.label} slider`}
          min={spec.min}
          max={spec.max}
          step={spec.step}
          value={pinned}
          onChange={(e) => onChange(e.target.value)}
          className="range"
          style={{ ["--fill" as string]: `${fill}%` }}
        />
      </Tooltip>
      <div className="num flex justify-between text-[10px] text-ink-3">
        <span>{spec.prefix}{spec.min.toLocaleString("en-GB")}{spec.unit}</span>
        <span>{spec.prefix}{spec.max.toLocaleString("en-GB")}{spec.unit}</span>
      </div>
    </div>
  );
}

/** A slider top that fits the account: the next 1, 2 or 5 above five times it. */
function capitalMax(balance: number | null): number {
  const target = Math.max(10_000, (balance ?? 0) * 5);
  const mag = 10 ** Math.floor(Math.log10(target));
  return ([1, 2, 5, 10].map((m) => m * mag).find((v) => v >= target)) ?? target;
}

export function CompoundInputsForm({ c, balance }: {
  c: CompoundCalculator; balance: number | null;
}) {
  // Losing days are some of the trading days, so their slider stops there.
  const days = c.inputs.daysPerWeek;
  const tradingDays = Number.isInteger(days) && days >= 1 && days <= 7 ? days : 7;
  const specs: Spec[] = [
    { name: "capital", label: "Starting capital", min: 100, max: capitalMax(balance), step: 100,
      prefix: "$", hint: "What the account starts with." },
    { name: "dailyPct", label: "Daily goal (%)", min: 0.05, max: 5, step: 0.05, unit: "%",
      hint: "Profit aimed for each trading day, as a percent of that day's balance." },
    { name: "daysPerWeek", label: "Trading days per week", min: 1, max: 7, step: 1,
      hint: "How many days a week the goal is traded for." },
    { name: "months", label: "Horizon (months)", min: 1, max: 60, step: 1,
      hint: "How far ahead to project." },
    { name: "reinvestPct", label: "Profit reinvested (%)", min: 0, max: 100, step: 5, unit: "%",
      hint: "Share of each day's profit left in the account. The rest is taken out and stops compounding." },
    { name: "monthlyDeposit", label: "Monthly deposit", min: 0, max: 5000, step: 50, prefix: "$",
      hint: "Paid in at the end of every month." },
    { name: "drawdownPct", label: "Max daily drawdown (%)", min: 0.05, max: 20, step: 0.05, unit: "%",
      hint: "Lost on each losing day, as a percent of that day's balance. The dollar figure moves with the account." },
    { name: "losingDays", label: "Losing days per week", min: 0, max: tradingDays, step: 1,
      hint: "How many of each week's trading days hit the max daily drawdown. They fall at the end of the week." },
  ];

  return (
    <div className="space-y-2">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {specs.map((s) => (
          <Slider key={s.name} spec={s} value={c.fields[s.name]}
            onChange={(v) => c.set(s.name, v)} />
        ))}
      </div>
      {c.canUseBalance && (
        <button
          type="button"
          onClick={c.resetToBalance}
          className={cn(
            "rounded border border-line px-2 py-1 text-[11px] text-ink-2 transition-colors",
            "hover:border-accent hover:text-ink-1",
          )}
        >
          Use account balance
        </button>
      )}
    </div>
  );
}
