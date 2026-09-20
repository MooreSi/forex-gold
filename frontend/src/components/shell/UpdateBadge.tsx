import { useState } from "react";
import { Sparkles } from "lucide-react";
import { UpdateDialog } from "./UpdateDialog";
import type { HeaderState } from "@/api/types";

interface UpdateBadgeProps {
  /** The header poll's `update` field. Null until it has answered. */
  update: NonNullable<HeaderState["update"]> | null | undefined;
}

/**
 * "UPDATE AVAILABLE" in the header, as the NiceGUI app had it.
 *
 * Hidden entirely unless a `git fetch` against origin has confirmed commits
 * this checkout does not have — a badge that appears on a maybe is one the
 * operator learns to ignore. Clicking it opens the popup that says what the
 * update changes and installs it.
 */
export function UpdateBadge({ update }: UpdateBadgeProps) {
  const [open, setOpen] = useState(false);
  if (update?.available !== true) return null;

  const n = update.commits;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={n > 0
          ? `${n} new commit${n === 1 ? "" : "s"} are waiting on GitHub. Click to see what changed.`
          : "A newer build is waiting on GitHub. Click to see what changed."}
        className="flex shrink-0 animate-pulse items-center gap-1 rounded border border-profit/50 bg-profit/15 px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-profit hover:bg-profit/25"
      >
        <Sparkles size={11} />
        <span>UPDATE AVAILABLE</span>
      </button>
      <UpdateDialog open={open} onOpenChange={setOpen} />
    </>
  );
}
