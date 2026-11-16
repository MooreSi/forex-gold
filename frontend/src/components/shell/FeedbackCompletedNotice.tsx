import { useState } from "react";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { api } from "@/api/client";
import { useHeaderState } from "@/hooks/useHeaderState";
import { invalidatePoll } from "@/hooks/usePoll";

const KIND_LABEL: Record<string, string> = {
  feature: "Feature request",
  bug: "Bug report",
  feedback: "General feedback",
};

/**
 * "Feedback noted and completed": shown to the person who sent feedback once
 * the owner marks it Completed in the admin console.
 *
 * The notices ride the shared header poll (`feedback_done`), so this costs no
 * interval of its own. One at a time, oldest first; Close tells the backend it
 * was seen, so each shows once. A failed post leaves it to show again on the
 * next poll rather than losing it.
 */
export function FeedbackCompletedNotice() {
  const { data } = useHeaderState();
  const [closing, setClosing] = useState<string | null>(null);
  const notice = (data?.feedback_done ?? []).find((n) => n.id !== closing) ?? null;

  const close = async () => {
    if (!notice) return;
    setClosing(notice.id);
    try {
      await api.post(`/api/feedback/notices/${encodeURIComponent(notice.id)}/seen`);
    } catch {
      setClosing(null);
      return;
    }
    invalidatePoll("system/header");
  };

  return (
    <DialogShell
      open={notice !== null}
      onOpenChange={(next) => { if (!next) void close(); }}
      title="Feedback noted and completed"
      description="Thanks for taking the time to send it."
      footer={<Button onClick={() => void close()}>Close</Button>}
    >
      {notice && (
        <div className="space-y-1">
          <p className="text-[10px] uppercase tracking-wide text-ink-3">
            {KIND_LABEL[notice.kind] ?? KIND_LABEL.feedback}
          </p>
          <p className="whitespace-pre-wrap text-xs leading-relaxed text-ink-2">
            {notice.message}
          </p>
        </div>
      )}
    </DialogShell>
  );
}
