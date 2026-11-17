import { useState } from "react";
import { MessageSquare } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { api, ApiError } from "@/api/client";
import type { FeedbackInboxRow } from "@/api/types";
import { invalidatePoll } from "@/hooks/usePoll";

const KIND_LABEL: Record<string, string> = {
  feature: "Feature request",
  bug: "Bug report",
  feedback: "General feedback",
};

interface FeedbackInboxBadgeProps {
  /** The header poll's `feedback_unread`. */
  count: number | undefined;
}

/**
 * "N NEW FEEDBACK" in the title bar of the admin node.
 *
 * AppHeader renders it only where the licence admin console is mounted, and
 * the backend counts only on the issuer, so a customer never sees it. Click
 * to read: the entries are fetched then (a message can be 4000 characters,
 * too much for the five-second header poll), and once shown those ids, and
 * only those, are marked read so the alert goes away. Reading does not mark
 * anything Completed; that stays in the admin console.
 */
export function FeedbackInboxBadge({ count }: FeedbackInboxBadgeProps) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<FeedbackInboxRow[] | null>(null);
  const [error, setError] = useState("");

  const openInbox = async () => {
    setOpen(true);
    setRows(null);
    setError("");
    try {
      const got = await api.get<FeedbackInboxRow[]>("/api/feedback/inbox");
      setRows(got);
      if (got.length > 0) {
        await api.post("/api/feedback/inbox/read", { ids: got.map((r) => r.id) });
        invalidatePoll("system/header");
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load the feedback.");
    }
  };

  if (!count && !open) return null;
  const n = count ?? 0;

  return (
    <>
      {n > 0 && (
        <button
          type="button"
          onClick={() => void openInbox()}
          title="Feedback has arrived. Click to read it."
          className="flex shrink-0 animate-pulse items-center gap-1 rounded border border-accent/50 bg-accent/15 px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-accent hover:bg-accent/25"
        >
          <MessageSquare size={11} aria-hidden />
          <span>{n} NEW FEEDBACK</span>
        </button>
      )}
      <DialogShell
        open={open}
        onOpenChange={setOpen}
        title="New feedback"
        description="Marked read now. Complete it from the licence admin console."
        footer={
          <>
            <a
              href="/admin/"
              className="rounded px-3 py-1.5 text-xs text-accent hover:bg-surface-3"
            >
              Open admin console
            </a>
            <Button onClick={() => setOpen(false)}>Close</Button>
          </>
        }
      >
        {error ? (
          <p role="alert" className="text-xs text-loss">{error}</p>
        ) : rows === null ? (
          <p className="text-xs text-ink-3">Loading...</p>
        ) : rows.length === 0 ? (
          <p className="text-xs text-ink-3">Nothing new.</p>
        ) : (
          <ul className="space-y-3">
            {rows.map((r) => (
              <li key={r.id} className="rounded border border-line bg-surface-1 p-2.5">
                <div className="mb-1 flex flex-wrap items-baseline gap-x-2 text-[10px] text-ink-3">
                  <span className="font-semibold uppercase tracking-wide text-ink-2">
                    {KIND_LABEL[r.kind] ?? KIND_LABEL.feedback}
                  </span>
                  <span>
                    {r.name || r.hostname || "Unknown"}
                    {r.email ? ` <${r.email}>` : ""}
                  </span>
                  <span>{new Date(r.submitted_at * 1000).toLocaleString()}</span>
                </div>
                <p className="whitespace-pre-wrap text-xs leading-relaxed text-ink-1">
                  {r.message}
                </p>
              </li>
            ))}
          </ul>
        )}
      </DialogShell>
    </>
  );
}
