import { useEffect, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";

/**
 * A short-lived line of text about something that just happened.
 *
 * It exists because the header had two of these and neither could ever go
 * away. Pressing Restart left "Restarting app in 5 seconds — reconnect your
 * browser shortly." on the bar permanently, and switching account left "Now
 * pointed at the Demo account…" beside it; both were reported on 2026-09-21 as
 * text that "never goes away after it has restarted".
 *
 * **Two ways out, and both are needed.** The page reloads itself once the
 * backend comes back (`AuthContext`'s heartbeat), which clears this with
 * everything else -- that is the normal path. The timeout is for when the app
 * does NOT come back: Stop, or a restart that failed. A notice with only the
 * reload behind it would be permanent in exactly the cases where it is wrong.
 *
 * The dismiss button is for the operator who has read it and wants the header
 * back.
 */
interface NoticeProps {
  tone?: "ok" | "bad";
  /** How long before it clears itself. */
  ttlMs?: number;
  onDismiss: () => void;
  children: ReactNode;
}

const DEFAULT_TTL_MS = 30_000;

export function Notice({
  tone = "ok", ttlMs = DEFAULT_TTL_MS, onDismiss, children,
}: NoticeProps) {
  const [gone, setGone] = useState(false);

  // **`children` is deliberately not a dependency.** It is a fresh object on
  // every render of the parent, and both parents poll -- so re-running on it
  // would restart the countdown every few seconds and the notice would never
  // expire, which is the bug this component exists to fix. The parents clear
  // their own state through `onDismiss`, so a notice whose text changes gets a
  // fresh mount anyway.
  useEffect(() => {
    setGone(false);
    const id = setTimeout(() => { setGone(true); onDismiss(); }, ttlMs);
    return () => clearTimeout(id);
  }, [ttlMs, onDismiss]);

  if (gone) return null;

  return (
    <span
      role={tone === "ok" ? "status" : "alert"}
      className={cn(
        "flex max-w-sm items-start gap-1 text-[11px]",
        tone === "ok" ? "text-profit" : "text-loss",
      )}
    >
      <span className="min-w-0">{children}</span>
      <button
        type="button"
        aria-label="Dismiss this message"
        onClick={onDismiss}
        className="shrink-0 rounded p-0.5 text-ink-3 transition-colors hover:text-ink-1"
      >
        <X size={11} aria-hidden />
      </button>
    </span>
  );
}
