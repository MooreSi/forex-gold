import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { Tooltip } from "@/components/shared/Tooltip";

/** Counts per place a `template:<name>` override is stored. Optional because
 *  the backend may grow another one; a missing count reads as zero. */
export interface References {
  schedule?: number;
  channel_assignments?: number;
  ai_recommendations?: number;
  risk_settings?: number;
}

interface RenameTemplateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  name: string;
  onRename: (from: string, to: string) => Promise<{ repointed: Record<string, number> }>;
}

/**
 * Rename one EA template.
 *
 * **A template's name is a foreign key nobody declared.** A strategy override
 * is stored as the string `template:<name>` in the trading schedule, on a
 * channel, in the AI's per-channel recommendation and in the global risk
 * settings. The backend repoints all four in one go — but a rename that moves
 * live trading configuration should never do it silently, so this asks the
 * backend where the template is named and says so before the operator
 * commits.
 *
 * What it deliberately does NOT claim to move is history. A closed trade
 * recorded under the old name stays that way; the P&L attribution on the
 * Analysis tab is built from that exact string.
 */
function describe(r: References): string {
  const parts: string[] = [];
  const windows = r.schedule ?? 0;
  const channels = r.channel_assignments ?? 0;
  const recs = r.ai_recommendations ?? 0;
  if (windows) parts.push(`${windows} schedule window${windows === 1 ? "" : "s"}`);
  if (channels) parts.push(`${channels} channel${channels === 1 ? "" : "s"}`);
  if (recs) parts.push(`${recs} AI recommendation${recs === 1 ? "" : "s"}`);
  if (r.risk_settings) parts.push("the global strategy");
  if (parts.length === 0) {
    return "Nothing else refers to it, so this only changes the name.";
  }
  const list = parts.length === 1
    ? parts[0]
    : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
  return `This template is named in ${list}. Renaming repoints all of it to `
    + `the new name — the same template keeps trading. Closed trades keep the `
    + `name they were placed under.`;
}

export function RenameTemplateDialog({
  open, onOpenChange, name, onRename,
}: RenameTemplateDialogProps) {
  const [value, setValue] = useState(name);
  const [refs, setRefs] = useState<References | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reopened on a different template, or on the same one after an edit: the
  // box starts from the name as it is now, not from whatever was typed last.
  useEffect(() => {
    if (!open) return;
    setValue(name);
    setError(null);
    setRefs(null);
    let cancelled = false;
    void api
      .get<{ references: References }>(
        `/api/trading/templates/${encodeURIComponent(name)}/references`)
      .then((r) => !cancelled && setRefs(r?.references ?? null))
      // A warning that could not be fetched must not block the rename; the
      // backend repoints regardless, and this line is advisory.
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [open, name]);

  const trimmed = value.trim();
  const ready = trimmed.length > 0 && trimmed !== name && !busy;

  const submit = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await onRename(name, trimmed);
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogShell
      open={open}
      onOpenChange={onOpenChange}
      title={`Rename ${name}`}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            onClick={() => void submit()}
            disabled={!ready}
            disabledReason={
              trimmed.length === 0
                ? "A new name is required."
                : trimmed === name
                  ? "That is the name it already has."
                  : null
            }
          >
            {busy ? "Renaming…" : "Rename"}
          </Button>
        </div>
      }
    >
      <div className="space-y-3 text-xs">
        <label className="block">
          <span className="mb-1 block text-[11px] text-ink-3">New name</span>
          {/* The name is a foreign key nobody declared -- see this file's own
              docstring. The hover help says so at the box, because that is
              where somebody is when they decide what to type. */}
          <Tooltip label={
            "The name is how the schedule, the channels, the AI recommendation "
            + "and the global strategy refer to this template. Renaming repoints "
            + "every one of them, so the same template keeps trading. Closed "
            + "trades keep the name they were placed under."
          }>
            <input
              aria-label="New name"
              value={value}
              autoFocus
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
              className="w-full rounded border border-line bg-surface-1 px-2 py-1.5
                         text-xs text-ink-1 outline-none focus:border-accent"
            />
          </Tooltip>
        </label>

        {refs && (
          <p data-testid="rename-references"
            className="rounded-md border border-line bg-surface-2/60 px-3 py-2
                       text-[11px] text-ink-3">
            {describe(refs)}
          </p>
        )}

        {error && (
          <p role="alert"
            className="rounded-md border border-loss/40 bg-loss/10 px-3 py-2
                       text-[11px] text-loss">
            {error}
          </p>
        )}
      </div>
    </DialogShell>
  );
}
