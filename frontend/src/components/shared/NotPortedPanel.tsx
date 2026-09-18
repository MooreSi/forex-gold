import { Construction } from "lucide-react";
import { PanelShell } from "./PanelShell";

interface NotPortedPanelProps {
  tab: string;
  /** The task in docs/todo/frontend/react-port/ that will fill this tab. */
  task: string;
  /** Where the NiceGUI original lives, so the port has a starting point. */
  origin: string;
}

/**
 * An honest placeholder for a tab the React port has not reached yet.
 *
 * The alternative — hiding the tab — reads as a lost feature. This says what
 * is missing, which task covers it and where the original behaviour is
 * described, so the gap is legible rather than mysterious.
 * See docs/todo/frontend/react-port/QUESTIONS.md Q1.
 */
export function NotPortedPanel({ tab, task, origin }: NotPortedPanelProps) {
  return (
    <PanelShell title={tab} subtitle="Not ported yet">
      <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
        <Construction className="text-warning" size={28} />
        <p className="text-sm text-ink-1">
          The {tab} tab has not been rebuilt in React yet.
        </p>
        <p className="max-w-md text-xs text-ink-3">
          Tracked by <span className="num text-ink-2">{task}</span>. The behaviour it replaces
          lives in <span className="num text-ink-2">{origin}</span> in the NiceGUI app.
        </p>
      </div>
    </PanelShell>
  );
}
