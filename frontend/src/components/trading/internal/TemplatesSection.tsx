import { useState } from "react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { cn } from "@/lib/cn";

interface TemplatesSectionProps {
  templates: Record<string, unknown>[];
  eaConnected: boolean;
  eaLastSeen: number | null;
  onSave: (name: string, values: Record<string, unknown>) => Promise<{ pushed: boolean }>;
  onDelete: (name: string) => void;
  onInstallBuiltin: () => void;
}

/**
 * EA templates: rule sets the MetaTrader EA runs natively.
 *
 * The push result is reported separately from the save. "Saved but not pushed"
 * means the values apply on the next signal — a different outcome from a failed
 * save, and one that must not read as an error.
 */
export function TemplatesSection({
  templates, eaConnected, eaLastSeen, onSave, onDelete, onInstallBuiltin,
}: TemplatesSectionProps) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [result, setResult] = useState<string | null>(null);

  const open = (name: string, values: Record<string, unknown>) => {
    setEditing(name);
    setResult(null);
    setDraft(JSON.stringify(values, null, 2));
  };

  const save = async () => {
    if (!editing) return;
    let values: Record<string, unknown>;
    try {
      values = JSON.parse(draft);
    } catch {
      setResult("That is not valid JSON, so nothing was saved.");
      return;
    }
    const { pushed } = await onSave(editing, values);
    setResult(
      pushed
        ? "Saved and pushed to the connected EA."
        : "Saved. No EA is connected, so these apply on the next signal.",
    );
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <span
          data-testid="ea-status"
          data-connected={eaConnected}
          className={cn("text-[11px]", eaConnected ? "text-profit" : "text-warning")}
        >
          {eaConnected
            ? `EA connected${eaLastSeen != null ? ` — last heard ${Math.round(eaLastSeen)}s ago` : ""}`
            : "No EA connected. Templates still save; they apply on the next signal."}
        </span>
        <Button variant="ghost" onClick={onInstallBuiltin} className="ml-auto">
          Restore the shipped preset
        </Button>
      </div>

      {templates.length === 0 ? (
        <EmptyState
          title="No EA templates saved"
          hint="Restore the shipped preset to start from a working one."
        />
      ) : (
        <ul className="space-y-1.5">
          {templates.map((t, i) => {
            const name = String(t["name"] ?? i);
            return (
              <li
                key={name}
                data-testid={`template-${name}`}
                className="flex items-center gap-2 rounded border border-line bg-surface-2 px-3 py-2"
              >
                <span className="text-xs text-ink-1">{name}</span>
                <Button variant="ghost" className="ml-auto" onClick={() => open(name, t)}>
                  Edit
                </Button>
                <Button variant="danger" onClick={() => onDelete(name)}>Delete</Button>
              </li>
            );
          })}
        </ul>
      )}

      {editing && (
        <div className="rounded border border-line bg-surface-1 p-3">
          <h4 className="text-xs font-semibold text-ink-1">{editing}</h4>
          <textarea
            aria-label={`${editing} values`}
            value={draft}
            rows={10}
            onChange={(e) => setDraft(e.target.value)}
            className="num mt-2 w-full rounded border border-line bg-surface-0 px-2 py-1 text-[11px] text-ink-1"
          />
          <div className="mt-2 flex items-center gap-2">
            <Button onClick={() => void save()}>Save</Button>
            <Button variant="ghost" onClick={() => setEditing(null)}>Close</Button>
            {result && <span role="status" className="text-[11px] text-ink-2">{result}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
