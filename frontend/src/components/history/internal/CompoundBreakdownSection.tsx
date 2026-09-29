import {
  formatCompactMoney, formatMoney, formatPercent,
} from "@/components/shared/format";
import { cn } from "@/lib/cn";
import type { Period, Projection } from "./compoundMath";

/**
 * The table the profit charts are drawn from, a row per month or per week.
 *
 * "Taken out" and "Paid in" appear only when there is some. Two columns of
 * $0.00 on every row are noise that pushes the columns that matter off a
 * narrow screen.
 */

/** Past a billion the digits stop being readable; the compact form is not. */
const money = (v: number) => (Math.abs(v) >= 1e9 ? formatCompactMoney(v) : formatMoney(v));

export function CompoundBreakdownSection({ projection, capital, view, onView }: {
  projection: Projection;
  capital: number;
  view: "months" | "weeks";
  onView: (v: "months" | "weeks") => void;
}) {
  const rows: Period[] = view === "months" ? projection.months : projection.weeks;
  const noun = view === "months" ? "Month" : "Week";
  const showOut = projection.totalWithdrawn > 0;
  const showIn = projection.totalDeposited > 0;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-[11px] uppercase tracking-wide text-ink-3">{noun} by {noun.toLowerCase()}</p>
        <div className="flex gap-1">
          {(["months", "weeks"] as const).map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={view === v}
              onClick={() => onView(v)}
              className={cn(
                "rounded px-2 py-1 text-[11px] transition-colors",
                view === v ? "bg-surface-3 text-ink-1" : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
              )}
            >
              {v === "months" ? "Monthly" : "Weekly"}
            </button>
          ))}
        </div>
      </div>


      <div className="max-h-80 overflow-auto rounded border border-line">
        <table className="w-full text-[11px]">
          <thead className="sticky top-0 bg-surface-2 text-ink-3">
            <tr>
              {[noun, "Start", "Profit", "Profit %",
                ...(showOut ? ["Taken out"] : []), ...(showIn ? ["Paid in"] : []),
                "End", "vs start"].map((h, i) => (
                <th key={h} className={cn("px-2 py-1.5 font-medium", i === 0 ? "text-left" : "text-right")}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="num">
            {rows.map((r) => (
              <tr key={r.index} className="border-t border-line/60 hover:bg-surface-2">
                <td className="px-2 py-1 text-left text-ink-2">{noun} {r.index}</td>
                <td className="px-2 py-1 text-right text-ink-2">{money(r.start)}</td>
                <td className="px-2 py-1 text-right text-profit">{money(r.gain)}</td>
                <td className="px-2 py-1 text-right text-profit">{formatPercent(r.gainPct, 2)}</td>
                {showOut && <td className="px-2 py-1 text-right text-warning">{money(r.withdrawn)}</td>}
                {showIn && <td className="px-2 py-1 text-right text-ink-2">{money(r.deposit)}</td>}
                <td className="px-2 py-1 text-right font-semibold text-ink-1">{money(r.end)}</td>
                <td className="px-2 py-1 text-right text-ink-3">
                  {formatPercent((r.end / capital - 1) * 100, 1)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
