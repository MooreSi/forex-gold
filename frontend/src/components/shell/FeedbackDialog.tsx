import { useState } from "react";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { api, ApiError } from "@/api/client";
import { cn } from "@/lib/cn";

const KINDS = [
  { id: "feature", label: "Feature request" },
  { id: "bug", label: "Bug" },
  { id: "feedback", label: "General feedback" },
] as const;

/** Keep in step with `MAX_MESSAGE` in `services/feedback/service.py`. */
const MAX_MESSAGE = 4000;

interface FeedbackDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Feature requests, bug reports and general feedback, sent to the owner.
 *
 * The backend queues it and delivers it over the admin connection, so a send
 * made while offline still arrives later: "Thanks" here means the app has it,
 * not that the owner has already read it, and the text says so.
 */
export function FeedbackDialog({ open, onOpenChange }: FeedbackDialogProps) {
  const [kind, setKind] = useState<string>("feature");
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");

  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      // Reset on the way out, so reopening is a fresh form and not last
      // time's confirmation.
      setSent(false);
      setError("");
      setMessage("");
      setKind("feature");
    }
  };

  const send = async () => {
    setSending(true);
    setError("");
    try {
      await api.post("/api/feedback", { kind, message });
      setSent(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not send. Try again in a moment.");
    } finally {
      setSending(false);
    }
  };

  const empty = message.trim().length === 0;

  return (
    <DialogShell
      open={open}
      onOpenChange={close}
      title="Feedback"
      description="Request a feature, report a bug, or tell us what you think."
      footer={
        sent ? (
          <Button onClick={() => close(false)}>Close</Button>
        ) : (
          <>
            <Button variant="ghost" onClick={() => close(false)}>Cancel</Button>
            <Button
              variant="success"
              onClick={() => void send()}
              disabledReason={
                sending ? "Sending." : empty ? "Write your feedback first." : null
              }
            >
              {sending ? "Sending..." : "Send"}
            </Button>
          </>
        )
      }
    >
      {sent ? (
        <p className="text-xs leading-relaxed text-ink-1">
          Thanks, your feedback has been sent.
        </p>
      ) : (
        <div className="space-y-3">
          <div role="radiogroup" aria-label="Type of feedback" className="flex gap-2">
            {KINDS.map((k) => (
              <button
                key={k.id}
                type="button"
                role="radio"
                aria-checked={kind === k.id}
                onClick={() => setKind(k.id)}
                className={cn(
                  "rounded border px-2.5 py-1 text-xs transition-colors",
                  kind === k.id
                    ? "border-accent bg-accent/15 text-ink-1"
                    : "border-line text-ink-3 hover:bg-surface-3 hover:text-ink-2",
                )}
              >
                {k.label}
              </button>
            ))}
          </div>
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            maxLength={MAX_MESSAGE}
            rows={7}
            placeholder="What would you like us to know?"
            aria-label="Your feedback"
            className="w-full resize-y rounded border border-line bg-surface-1 p-2 text-xs text-ink-1 placeholder:text-ink-3 focus:border-accent focus:outline-none"
          />
          {error && <p role="alert" className="text-xs text-loss">{error}</p>}
        </div>
      )}
    </DialogShell>
  );
}
