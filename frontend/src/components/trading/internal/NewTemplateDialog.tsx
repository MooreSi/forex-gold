import { useEffect, useState } from "react";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { Tooltip } from "@/components/shared/Tooltip";

interface NewTemplateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Names already taken. Saving is an upsert, so a clash would overwrite. */
  existing: string[];
  onCreate: (name: string) => Promise<void>;
}

/**
 * Name a new EA template. It is created with every field at its default and
 * then opened in the editor; nothing about it is tuned until the operator
 * does that. An existing name is refused here because the save endpoint
 * overwrites, and a "new" template that silently replaced a tuned one would
 * be the worst possible reading of this button.
 */
export function NewTemplateDialog({
  open, onOpenChange, existing, onCreate,
}: NewTemplateDialogProps) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setValue("");
    setError(null);
  }, [open]);

  const trimmed = value.trim();
  const taken = existing.some((n) => n.trim().toLowerCase() === trimmed.toLowerCase());
  const reason = trimmed.length === 0
    ? "A name is required."
    : taken ? "A template with that name already exists." : null;
  const ready = reason === null && !busy;

  const submit = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await onCreate(trimmed);
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
      title="New EA template"
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => void submit()} disabled={!ready} disabledReason={reason}>
            {busy ? "Creating…" : "Create"}
          </Button>
        </div>
      }
    >
      <div className="space-y-3 text-xs">
        <label className="block">
          <span className="mb-1 block text-[11px] text-ink-3">Template name</span>
          <Tooltip label="What this template is called in the list and in every channel or schedule that points at it. It must not match an existing template: saving overwrites by name.">
            <input
              aria-label="Template name"
              value={value}
              autoFocus
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
              className="w-full rounded border border-line bg-surface-1 px-2 py-1.5
                         text-xs text-ink-1 outline-none focus:border-accent"
            />
          </Tooltip>
        </label>
        <p className="text-[11px] text-ink-3">
          Starts with every setting at its default. It opens for editing next.
        </p>
        {taken && trimmed && (
          <p className="text-[11px] text-warning">{reason}</p>
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
