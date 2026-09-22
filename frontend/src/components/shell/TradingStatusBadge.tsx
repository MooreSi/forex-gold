import { useCallback, useEffect, useState } from "react";
import { Ban, HelpCircle, Newspaper, Shield, Trophy } from "lucide-react";
import { TradingStatusDialog } from "./TradingStatusDialog";
import type { TradingStatus } from "./TradingStatusDialog";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { cn } from "@/lib/cn";

/**
 * Is trading actually running right now?
 *
 * Four separate mechanisms can hold an automated entry, and an operator should
 * not have to visit four screens to find out which one is doing it. The
 * backend decides which to report and in what order — this renders the answer
 * and never computes it, because a second opinion about a risk state produces
 * two answers that drift.
 *
 * Clicking it opens the one control for all of this (`TradingStatusDialog`):
 * a Resume when one would actually do something, and the pause form when
 * nothing is holding entries. Until 2026-09-22 the header also carried a
 * separate Pause button, which knew about one of the four mechanisms and so
 * offered "Pause" while a profit target already held every entry.
 */
const LOOK: Record<string, { Icon: typeof Shield; className: string }> = {
  ok: { Icon: Shield, className: "text-profit" },
  halted: { Icon: Ban, className: "text-loss" },
  profit_target: { Icon: Trophy, className: "text-accent" },
  news_blackout: { Icon: Newspaper, className: "text-warning" },
  unknown: { Icon: HelpCircle, className: "text-ink-3" },
};

/** "4:37" — the time left, counted in the browser against its own clock. */
function countdown(resumeTs: number | null, now: number): string {
  if (resumeTs === null) return "";
  const left = resumeTs - now / 1000;
  if (left <= 0) return "";
  return ` — ${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}`;
}

export function TradingStatusBadge() {
  const { data, refresh } = usePoll<TradingStatus>(
    "trading/status-badge",
    useCallback(() => api.get<TradingStatus>("/api/trading/status-badge"), []),
    5_000,
  );
  const [open, setOpen] = useState(false);
  // Ticks once a second purely so a blackout countdown moves between the
  // five-second polls, instead of freezing at whatever the last one said.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);

  if (!data) return null;

  const look = LOOK[data.state] ?? LOOK.unknown;
  const { Icon } = look;

  return (
    <div className="relative shrink-0">
      <button
        type="button"
        data-testid="trading-status-badge"
        title={data.detail || data.label}
        onClick={() => setOpen(true)}
        className={cn(
          "flex items-center gap-1.5 border-l border-line px-3 text-xs font-semibold",
          look.className,
        )}
      >
        <Icon size={13} aria-hidden />
        {data.label}
        {data.state === "news_blackout" && countdown(data.resume_ts, now)}
      </button>

      <TradingStatusDialog
        open={open}
        onOpenChange={setOpen}
        status={data}
        onChanged={() => void refresh()}
      />
    </div>
  );
}
