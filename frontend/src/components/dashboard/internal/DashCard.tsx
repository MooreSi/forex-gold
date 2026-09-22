import type { ReactNode } from "react";
import { iconFor } from "@/components/shared/icons";
import { cn } from "@/lib/cn";

interface DashCardProps {
  title: string;
  /** A name from `shared/icons.ts`. An unknown name renders no icon rather
   *  than a stand-in that would mean something else. */
  icon?: string;
  /** Small, dim, right of the title. A state or a window, not a sentence. */
  badge?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** One line under the card's body, for what the figures above it mean. */
  footnote?: ReactNode;
}

/**
 * The one card chrome on this screen.
 *
 * `PanelShell` is the chrome for a TAB — it owns a screen surface, scrolls its
 * own body and is sized to fill the tab. The Dashboard is a grid of a dozen
 * small readings inside one of those, and giving each of them a PanelShell
 * produced a header bar per reading and a scroll container per reading. This
 * is the smaller unit: same border, same surface, same title scale, no
 * scrolling of its own.
 *
 * It is `internal/` because it is chrome for this grid and nothing else. A
 * second screen that wants it is the signal to promote it to `shared/`, not
 * to import it across a domain boundary.
 */
export function DashCard({
  title, icon, badge, actions, children, className, footnote,
}: DashCardProps) {
  const Icon = iconFor(icon);
  return (
    <section
      className={cn(
        "flex min-w-0 flex-col rounded-lg border border-line bg-surface-1",
        "shadow-[0_1px_2px_rgba(0,0,0,0.18)]",
        className,
      )}
    >
      <header className="flex shrink-0 items-center justify-between gap-2 border-b
                         border-line/70 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          {Icon && (
            <span
              aria-hidden
              className="flex size-6 shrink-0 items-center justify-center rounded-md
                         bg-accent/10 text-accent"
            >
              <Icon size={13} />
            </span>
          )}
          <h3 className="truncate text-xs font-semibold text-ink-1">{title}</h3>
          {badge}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
      </header>
      <div className="min-w-0 flex-1 px-3 py-2.5">{children}</div>
      {footnote && (
        <p className="border-t border-line/70 px-3 py-1.5 text-[10px] text-ink-3">
          {footnote}
        </p>
      )}
    </section>
  );
}

/** A label above a value, the unit this grid is built from. */
export function Reading({ label, value, tone, hint }: {
  label: string;
  value: ReactNode;
  /** A colour class, usually from `pnlColour()`. */
  tone?: string;
  hint?: string;
}) {
  return (
    <div className="min-w-0" title={hint}>
      <p className="truncate text-[9px] uppercase tracking-wider text-ink-3">{label}</p>
      <p className={cn("num truncate text-sm font-semibold text-ink-1", tone)}>{value}</p>
    </div>
  );
}

/** A small state pill. Colour carries the same meanings as everywhere else. */
export function Pill({ tone = "neutral", children }: {
  tone?: "profit" | "loss" | "warning" | "accent" | "neutral";
  children: ReactNode;
}) {
  const tones: Record<string, string> = {
    profit: "bg-profit/15 text-profit",
    loss: "bg-loss/15 text-loss",
    warning: "bg-warning/15 text-warning",
    accent: "bg-accent/15 text-accent",
    neutral: "bg-surface-3 text-ink-3",
  };
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", tones[tone])}>
      {children}
    </span>
  );
}
