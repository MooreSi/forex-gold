import { MessageSquarePlus } from "lucide-react";
import { useState } from "react";
import { FeedbackDialog } from "./FeedbackDialog";

/**
 * The "Feedback" button at the end of the tab strip, straight after About.
 * A button, not a tab: it opens a popup over whatever you are looking at
 * rather than replacing it.
 */
export function FeedbackButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="-mb-px flex items-center gap-1.5 whitespace-nowrap border-b-2 border-transparent px-3 py-2 text-xs text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink-2"
      >
        <MessageSquarePlus size={14} aria-hidden />
        Feedback
      </button>
      <FeedbackDialog open={open} onOpenChange={setOpen} />
    </>
  );
}
