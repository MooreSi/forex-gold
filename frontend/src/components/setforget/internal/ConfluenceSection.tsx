import { Check, Minus } from "lucide-react";
import type { Confluence } from "@/api/types";
import { cn } from "@/lib/cn";

const GRADE: Record<Confluence["grade"], { label: string; tone: string; bar: string }> = {
  high: { label: "High confluence", tone: "text-profit", bar: "bg-profit" },
  moderate: { label: "Moderate confluence", tone: "text-warning", bar: "bg-warning" },
  low: { label: "Low confluence", tone: "text-loss", bar: "bg-loss" },
};

/**
 * The scored checklist, and every item's reason.
 *
 * The reasons are the product, not the number. A panel showing 7/9 with no
 * account of which two failed tells the operator nothing they can act on, and
 * a score with nothing behind it is exactly how a checklist turns into a
 * rubber stamp.
 *
 * Failed items are not hidden or greyed into the background. They are the ones
 * worth reading.
 */
export function ConfluenceSection({ confluence }: { confluence: Confluence }) {
  const grade = GRADE[confluence.grade] ?? GRADE.low;

  return (
    <section className="rounded-lg border border-line bg-surface-1 p-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-xs font-semibold text-ink-1">Confluence checklist</h3>
        <p className="flex items-baseline gap-2">
          <span className={cn("text-xs font-semibold", grade.tone)}>{grade.label}</span>
          <span className="num text-[11px] text-ink-3">
            {confluence.score} / {confluence.max}
          </span>
        </p>
      </header>

      <div
        className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-3"
        role="meter"
        aria-valuenow={confluence.score}
        aria-valuemin={0}
        aria-valuemax={confluence.max}
        aria-label="Confluence score"
      >
        <div
          className={cn("h-full rounded-full transition-all", grade.bar)}
          style={{ width: `${Math.max(confluence.pct, 2)}%` }}
        />
      </div>

      <ul className="mt-3 space-y-1.5">
        {confluence.items.map((item) => (
          <li
            key={item.id}
            className={cn(
              "flex gap-2 rounded-md border px-2.5 py-2",
              item.passed
                ? "border-profit/25 bg-profit/[0.06]"
                : "border-line bg-surface-2/50",
            )}
          >
            <span
              className={cn(
                "mt-px flex h-4 w-4 shrink-0 items-center justify-center rounded-full",
                item.passed ? "bg-profit/20 text-profit" : "bg-surface-3 text-ink-3",
              )}
            >
              {item.passed ? <Check size={11} /> : <Minus size={11} />}
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-baseline justify-between gap-2">
                <span className={cn("text-[11px] font-medium",
                                    item.passed ? "text-ink-1" : "text-ink-2")}>
                  {item.label}
                </span>
                <span className="num shrink-0 text-[10px] text-ink-3">
                  {item.passed ? `+${item.weight}` : `0 / ${item.weight}`}
                </span>
              </span>
              <span className="mt-0.5 block text-[10px] leading-relaxed text-ink-3">
                {item.detail}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
