import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

interface StatCardProps {
  label: string;
  value: ReactNode;
  /** Colour class for the value, e.g. from `pnlColour()`. */
  valueClassName?: string;
  hint?: string;
}

/** A label above a number. The number is monospaced so two of these line up. */
export function StatCard({ label, value, valueClassName, hint }: StatCardProps) {
  return (
    <div className="rounded border border-line bg-surface-2 px-3 py-2" title={hint}>
      <div className="text-[10px] uppercase tracking-wide text-ink-3">{label}</div>
      <div className={cn("num mt-0.5 text-sm font-semibold text-ink-1", valueClassName)}>
        {value}
      </div>
    </div>
  );
}
