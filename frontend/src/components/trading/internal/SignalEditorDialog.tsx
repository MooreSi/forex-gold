import { useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { DialogShell } from "@/components/shared/DialogShell";

interface SignalEditorDialogProps {
  signal: Record<string, unknown> | null;
  onClose: () => void;
  onSaved: () => void;
}

const FIELDS: { key: string; label: string; help: string }[] = [
  { key: "entry_low", label: "Entry low",
    help: "The near end of the entry band. A signal quotes a range; the position is opened anywhere inside it." },
  { key: "entry_high", label: "Entry high",
    help: "The far end of the entry band. Set it equal to Entry low to make the signal a single price." },
  { key: "stop_loss", label: "Stop loss",
    help: "Where the position is closed for a loss. This is the number position sizing is derived from, so moving it changes how big the trade will be." },
  { key: "tp1", label: "TP1",
    help: "The first take-profit. Scale-out strategies close part of the position here and manage the rest." },
  { key: "tp2", label: "TP2",
    help: "The second take-profit. Leave it blank if the signal does not carry one — a blank is not the same as 0, which is a real price." },
  { key: "tp3", label: "TP3",
    help: "The third take-profit. A signal can carry up to eight; the ones past TP3 are left as the signal sent them." },
];

/**
 * Edit ONE pending signal's levels.
 *
 * The id is taken from the signal this dialog was opened with and sent in the
 * URL, never in the body. The NiceGUI editor read fifteen widgets out of an
 * enclosing loop, so without explicit captures Save on one row wrote another
 * row's values; React's version of that mistake is a handler closing over
 * state from an earlier render. Keying the dialog on the signal id — so it
 * remounts per row — and putting the id in the path are the two defences, and
 * `TradingPanel` tests that editing one row leaves the others alone.
 */
export function SignalEditorDialog({ signal, onClose, onSaved }: SignalEditorDialogProps) {
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      FIELDS.map((f) => [f.key, signal?.[f.key] == null ? "" : String(signal[f.key])]),
    ),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const id = String(signal?.["signal_id"] ?? signal?.["id"] ?? "");

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const body: Record<string, number | null> = {};
      for (const f of FIELDS) {
        const raw = draft[f.key]?.trim() ?? "";
        body[f.key] = raw === "" ? null : Number(raw);
      }
      await api.put(`/api/trading/signals/${encodeURIComponent(id)}`, body);
      onSaved();
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <DialogShell
      open={signal !== null}
      onOpenChange={(open) => !open && onClose()}
      title={`Edit signal ${id}`}
      description="Changes apply to this signal only. It has not been executed yet."
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button onClick={() => void save()} disabled={saving}>
            {saving ? "Saving…" : "Save this signal"}
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-2 gap-3">
        {FIELDS.map((f) => (
          <label key={f.key} className="block">
            <span className="text-xs text-ink-2">{f.label}</span>
            <Tooltip label={f.help}>
              <input
                aria-label={f.label}
                inputMode="decimal"
                value={draft[f.key] ?? ""}
                onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                className="num mt-1 w-full rounded border border-line bg-surface-1 px-2 py-1.5 text-sm text-ink-1"
              />
            </Tooltip>
          </label>
        ))}
      </div>
      {error && <p role="alert" className="mt-3 text-xs text-loss">{error}</p>}
    </DialogShell>
  );
}
