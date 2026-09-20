import { useEffect } from "react";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { useUpdateCheck } from "@/hooks/useUpdateCheck";

interface UpdateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * What the pending update changes, and the button that installs it.
 *
 * The plain-English lines come from the AI provider and the commit subjects
 * are the fallback — which is not a degraded mode: "what changed?" answered
 * with the commit subjects is still a truer answer than "an update is
 * available". **Neither is a precondition for updating.** The summary costs a
 * paid model call that can fail for reasons that have nothing to do with the
 * release, so Update Now is never gated on it.
 *
 * The detail is fetched when this opens, not when the badge appears: opening
 * is the only moment the summary is worth paying for.
 */
export function UpdateDialog({ open, onOpenChange }: UpdateDialogProps) {
  const upd = useUpdateCheck(true);
  const { check } = upd;

  useEffect(() => {
    if (open) void check();
  }, [open, check]);

  const check_ = upd.status?.update;
  const commits = check_?.commits ?? [];
  const summary = upd.status?.changes ?? [];
  const lines = summary.length > 0 ? summary : commits.map((c) => c.summary);

  return (
    <DialogShell
      open={open}
      onOpenChange={onOpenChange}
      title="Update available"
      description={
        commits.length > 0
          ? `${commits.length} new commit${commits.length === 1 ? "" : "s"} from github.com/MooreSi/forex.`
          : "An update is ready from github.com/MooreSi/forex."
      }
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            variant="success"
            onClick={() => void upd.apply()}
            disabledReason={upd.applying ? "The update is running." : null}
          >
            {upd.applying ? "Updating..." : "Update now"}
          </Button>
        </>
      }
    >
      {upd.checking && lines.length === 0 && (
        <p className="text-xs italic text-ink-3">Summarising what has changed...</p>
      )}

      {lines.length > 0 && (
        <ul data-testid="update-changes" className="space-y-1">
          {lines.map((line, i) => (
            <li key={i} className="flex gap-2 text-xs leading-relaxed text-ink-1">
              <span className="text-profit">•</span>
              <span>{line}</span>
            </li>
          ))}
        </ul>
      )}

      {!upd.checking && lines.length === 0 && (
        <p className="text-xs text-ink-3">
          The commit list for this update could not be read.
        </p>
      )}

      {summary.length === 0 && upd.status?.changes_error && (
        <p className="mt-2 text-[11px] italic text-ink-3">
          Plain-English summary unavailable — {upd.status.changes_error}
        </p>
      )}

      {/* Only when the bullets above are NOT the commit subjects — otherwise
          this is the same list twice, once under a heading that implies it
          says something the bullets did not. */}
      {commits.length > 0 && summary.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-[11px] text-ink-3">Commit list</summary>
          <div className="mt-1 max-h-48 space-y-0.5 overflow-y-auto">
            {commits.map((c) => (
              <p key={c.sha} className="num text-[11px] leading-relaxed text-ink-2">
                {c.short_sha}  {c.summary}
              </p>
            ))}
          </div>
        </details>
      )}

      {upd.error && <p className="mt-2 text-[11px] text-warning">{upd.error}</p>}
      {upd.applying && (
        <p className="mt-2 text-[11px] text-warning">
          Updating — pulling the latest code and reinstalling dependencies...
        </p>
      )}
      {upd.applyError && (
        <p className="mt-2 text-[11px] text-loss">Update failed: {upd.applyError}</p>
      )}
      {upd.applied && (
        <p className="mt-2 text-[11px] text-profit">Update applied — restarting...</p>
      )}
    </DialogShell>
  );
}
