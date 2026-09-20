import { useCallback, useEffect, useState } from "react";
import { Ban, HelpCircle, Newspaper, Shield, Trophy } from "lucide-react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { cn } from "@/lib/cn";

interface BadgeState {
  state: string;
  label: string;
  detail: string;
  until: number | null;
  resume_ts: number | null;
  can_resume: boolean;
}

/**
 * Is trading actually running right now?
 *
 * Four separate mechanisms can hold an automated entry, and an operator should
 * not have to visit four screens to find out which one is doing it. The
 * backend decides which to report and in what order — this renders the answer
 * and never computes it, because a second opinion about a risk state produces
 * two answers that drift.
 *
 * Clicking it offers a Resume, and only when one would actually do something:
 * a news blackout lifts itself, so it is shown without a button.
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
  const { data, refresh } = usePoll<BadgeState>(
    "trading/status-badge",
    useCallback(() => api.get<BadgeState>("/api/trading/status-badge"), []),
    5_000,
  );
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
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

  const resume = async () => {
    setBusy(true);
    try {
      await api.post("/api/trading/resume-all");
      await refresh();
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative shrink-0">
      <button
        type="button"
        data-testid="trading-status-badge"
        title={data.detail || data.label}
        onClick={() => setConfirming((v) => !v)}
        className={cn(
          "flex items-center gap-1.5 border-l border-line px-3 text-xs font-semibold",
          look.className,
        )}
      >
        <Icon size={13} aria-hidden />
        {data.label}
        {data.state === "news_blackout" && countdown(data.resume_ts, now)}
      </button>

      {confirming && (
        <div
          data-testid="resume-confirm"
          className="absolute right-0 top-full z-50 mt-1 w-72 rounded-lg border border-line bg-surface-1 p-3 shadow-lg"
        >
          <p className="mb-1 text-sm font-semibold text-ink-1">
            {data.can_resume ? "Re-enable trading?" : data.label}
          </p>
          <p className="mb-3 text-[11px] text-ink-3">
            {data.detail || "Trading is not currently paused."}
          </p>
          {data.can_resume ? (
            <div className="flex gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => void resume()}
                className="rounded border border-profit/40 bg-profit/15 px-3 py-1 text-xs text-profit disabled:opacity-50"
              >
                {busy ? "Resuming…" : "Resume Trading"}
              </button>
              <button
                type="button"
                onClick={() => setConfirming(false)}
                className="rounded border border-line bg-surface-2 px-3 py-1 text-xs text-ink-2"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded border border-line bg-surface-2 px-3 py-1 text-xs text-ink-2"
            >
              Close
            </button>
          )}
        </div>
      )}
    </div>
  );
}
