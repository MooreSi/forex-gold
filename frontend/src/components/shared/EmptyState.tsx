import type { ReactNode } from "react";

interface EmptyStateProps {
  title: string;
  /** What to do next. An empty panel that does not say why is a bug report. */
  hint?: ReactNode;
  icon?: ReactNode;
}

export function EmptyState({ title, hint, icon }: EmptyStateProps) {
  return (
    <div className="flex h-full min-h-32 flex-col items-center justify-center gap-2 text-center">
      {icon && <div className="text-ink-3">{icon}</div>}
      <p className="text-sm text-ink-2">{title}</p>
      {hint && <p className="max-w-sm text-xs text-ink-3">{hint}</p>}
    </div>
  );
}
