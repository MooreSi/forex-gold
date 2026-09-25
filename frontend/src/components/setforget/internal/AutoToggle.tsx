import { Bot } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { formatClock } from "@/components/shared/format";
import { useSetForgetAuto } from "../hooks/useSetForgetAuto";

/**
 * The "Auto" button and its last scan. On, the backend reviews the market
 * with the AI every 15 minutes for a LONG and places it when the rules and the
 * AI agree -- demo account only, at most two a day, one open at a time. Those
 * refusals are server-side (`services/setforget/auto.py`); this switch is all
 * the page holds.
 */
export function AutoToggle() {
  const a = useSetForgetAuto();
  const on = Boolean(a.status?.enabled);
  return (
    <Button
      variant={on ? "success" : "primary"}
      aria-pressed={on}
      onClick={() => void a.toggle()}
      disabled={a.busy}
      tooltip={on
        ? "Auto is ON: every 15 minutes the AI reviews gold for a long setup "
          + "and places it (demo account only, max 2 a day). Click to stop."
        : "Turn on to have the AI review gold every 15 minutes and place a long "
          + "setup when there is one (demo account only, max 2 a day)."}
    >
      <Bot size={12} />
      {on ? "Auto: ON" : "Auto"}
    </Button>
  );
}

/** The line under the buttons: what Auto last decided, and why. */
export function AutoStatusLine() {
  const a = useSetForgetAuto();
  const s = a.status;
  if (a.error) {
    return <p role="alert" className="text-[11px] text-loss">{a.error}</p>;
  }
  if (!s?.enabled) return null;
  return (
    <p role="status" className="rounded-md border border-line bg-surface-2/60
                                px-3 py-2 text-[11px] text-ink-2">
      Auto is on, scanning for a long every {Math.round(s.interval_s / 60)} min.
      {s.last_run
        ? ` Last scan ${formatClock(s.last_run)}: ${s.reason}`
        : " First scan within a minute."}
    </p>
  );
}
