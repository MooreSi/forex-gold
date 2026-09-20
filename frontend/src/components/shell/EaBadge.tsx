import { useState } from "react";
import { CircleDot } from "lucide-react";
import type { EaBadge as EaBadgeState } from "@/api/types";
import { EaDialog } from "./EaDialog";
import { cn } from "@/lib/cn";

// The service names a colour; this is the only place that decides what the
// name looks like. Amber is "connected but stale", which is neither green nor
// red on purpose.
const EA_COLOURS: Record<string, string> = {
  green: "text-profit",
  red: "text-loss",
  orange: "text-warning",
  amber: "text-warning",
  grey: "text-ink-3",
  gray: "text-ink-3",
};

/**
 * The header's EA badge, and the way out of a stale build.
 *
 * Colour and words come from the backend: a stale EA shown as a green badge is
 * the screen contradicting the log, which is the bug `ea_badge_state` was
 * extracted for. What this adds is the click. The badge has reported staleness
 * since 2026-09-09 and left the operator to find `tools/deploy_ea.sh` on their
 * own; now it opens the dialog that does it.
 *
 * Clickable ONLY when stale. A button that opens a dialog saying "nothing is
 * wrong" teaches people the badge is noise.
 */
export function EaBadge({ badge }: { badge: EaBadgeState | null | undefined }) {
  const [open, setOpen] = useState(false);
  if (!badge) return null;

  const tone = EA_COLOURS[badge.colour] ?? "text-ink-3";
  const className = cn("flex shrink-0 items-center gap-1 text-[10px] font-semibold", tone);

  if (!badge.stale) {
    return (
      <span data-testid="ea-badge" className={className} title={badge.tooltip}>
        <CircleDot size={13} />
        {badge.text}
      </span>
    );
  }

  return (
    <>
      <button
        type="button"
        data-testid="ea-badge"
        onClick={() => setOpen(true)}
        title={`${badge.tooltip} — click to fix it.`}
        className={cn(className, "rounded border border-warning/50 bg-warning/10 px-1 py-0.5 hover:bg-warning/20")}
      >
        <CircleDot size={13} />
        {badge.text}
      </button>
      <EaDialog open={open} onOpenChange={setOpen} detail={badge.tooltip} />
    </>
  );
}
