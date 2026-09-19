import type { ReactNode } from "react";
import { iconFor } from "./icons";
import { cn } from "@/lib/cn";

interface PanelShellProps {
  title?: ReactNode;
  /** A name from `shared/icons.ts`. A name with no icon renders none, rather
   *  than a stand-in that would mean something else. */
  icon?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** Rendered small and dim under the title. Three words, not a paragraph. */
  subtitle?: ReactNode;
}

/**
 * Every Panel wraps this: one header, one padding scale, one scroll
 * behaviour. If two panels look different, that is a bug in one of them.
 */
export function PanelShell({
  title, subtitle, actions, children, className, icon,
}: PanelShellProps) {
  const Icon = iconFor(icon);
  return (
    <section
      className={cn(
        "flex min-h-0 flex-col overflow-hidden rounded-lg border border-line bg-surface-1",
        className,
      )}
    >
      {(title || actions) && (
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-line bg-surface-2/40 px-4 py-2.5">
          <div className="flex min-w-0 items-center gap-2.5">
            {Icon && (
              <span
                aria-hidden
                className="flex size-7 shrink-0 items-center justify-center rounded-md bg-accent/10 text-accent"
              >
                <Icon size={15} />
              </span>
            )}
            <div className="min-w-0">
              {title && <h2 className="truncate text-sm font-semibold text-ink-1">{title}</h2>}
              {subtitle && <p className="truncate text-xs text-ink-3">{subtitle}</p>}
            </div>
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="min-h-0 flex-1 overflow-auto p-4">{children}</div>
    </section>
  );
}
