import { useEffect, useMemo, useState } from "react";
import { Plug, PlugZap, Search } from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { EmptyState } from "@/components/shared/EmptyState";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { TemplateEditor, type SchemaField } from "./TemplateEditor";
import { TemplateTransfer } from "./TemplateTransfer";

interface TemplatesSectionProps {
  templates: Record<string, unknown>[];
  eaConnected: boolean;
  eaLastSeen: number | null;
  onSave: (name: string, values: Record<string, unknown>) => Promise<{ pushed: boolean }>;
  onDelete: (name: string) => void;
  onInstallBuiltin: () => void;
  /** Reload the list after an import wrote something. */
  onImported: () => void;
}

/**
 * EA templates: rule sets the MetaTrader EA runs natively.
 *
 * **Rearranged 2026-09-19.** It was a flat list of names, each with an Edit
 * button that opened a ten-row textarea holding the whole template as raw
 * JSON. With around a hundred keys that is not an editor, which is what the
 * owner meant by "i'm unable to edit a template".
 *
 * Now: the templates down the left, the form for the selected one filling the
 * rest. A template list that is two dozen long wants a filter, and the form
 * has its own search for the fields.
 *
 * The push result is still reported separately from the save. "Saved but not
 * pushed" means the values apply on the next signal — a different outcome
 * from a failed save, and one that must not read as an error.
 */
export function TemplatesSection({
  templates, eaConnected, eaLastSeen, onSave, onDelete, onInstallBuiltin,
  onImported,
}: TemplatesSectionProps) {
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [schema, setSchema] = useState<SchemaField[] | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  // Content, not state: the field list ships with the build and cannot change
  // while the app is running, so it is fetched once rather than polled.
  useEffect(() => {
    let cancelled = false;
    void api
      .get<{ fields: SchemaField[] }>("/api/trading/templates/schema")
      .then((r) => !cancelled && setSchema(asArray<SchemaField>(r?.fields)))
      .catch((e: Error) => !cancelled && setSchemaError(e.message));
    return () => { cancelled = true; };
  }, []);

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return templates.filter((t) => !q || String(t["name"] ?? "").toLowerCase().includes(q));
  }, [templates, filter]);

  const current = templates.find((t) => String(t["name"]) === selected) ?? null;

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <span
          data-testid="ea-status"
          data-connected={eaConnected}
          className={cn("flex items-center gap-1.5 text-[11px]",
            eaConnected ? "text-profit" : "text-warning")}
        >
          {eaConnected ? <PlugZap size={13} /> : <Plug size={13} />}
          {eaConnected
            ? `EA connected${eaLastSeen != null ? ` — last heard ${Math.round(eaLastSeen)}s ago` : ""}`
            : "No EA connected. Templates still save; they apply on the next signal."}
        </span>
        <Button variant="ghost" onClick={onInstallBuiltin} className="ml-auto">
          Restore the shipped preset
        </Button>
      </div>

      {/* Import/Export sit above the list, not beside a selected template:
          Export is "all of them" and Import may add several, so neither is
          an operation on the one that happens to be open. Delete stays at
          the bottom, with the template it deletes named on the button. */}
      <TemplateTransfer onImported={onImported} />

      {templates.length === 0 ? (
        <EmptyState
          title="No EA templates saved"
          hint="Restore the shipped preset to start from a working one."
        />
      ) : (
        <div className="grid min-h-0 gap-3 lg:grid-cols-[16rem_1fr]">
          <div className="flex min-h-0 flex-col rounded-lg border border-line bg-surface-1">
            <div className="flex shrink-0 items-center gap-1.5 border-b border-line px-2 py-1.5">
              <Search size={12} className="text-ink-3" aria-hidden />
              <Tooltip label="Narrow the list to templates whose name contains what you type. It only hides rows — nothing is deleted and nothing stops being used by the EA.">
                <input
                  aria-label="Filter templates"
                  placeholder="Filter templates"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  className="w-full bg-transparent text-[11px] text-ink-1 outline-none"
                />
              </Tooltip>
            </div>
            <ul className="min-h-0 flex-1 overflow-auto p-1">
              {shown.map((t, i) => {
                const name = String(t["name"] ?? i);
                const active = name === selected;
                return (
                  <li key={name} data-testid={`template-${name}`}>
                    <button
                      type="button"
                      onClick={() => setSelected(active ? null : name)}
                      aria-pressed={active}
                      className={cn(
                        "w-full truncate rounded px-2 py-1.5 text-left text-xs transition-colors",
                        active ? "bg-surface-3 text-ink-1" : "text-ink-2 hover:bg-surface-2",
                      )}
                    >
                      {name}
                    </button>
                  </li>
                );
              })}
              {shown.length === 0 && (
                <li className="px-2 py-1.5 text-[11px] text-ink-3">
                  No template matches “{filter}”.
                </li>
              )}
            </ul>
          </div>

          <div className="min-h-0">
            {!current ? (
              <EmptyState
                title="Pick a template to edit it"
                hint="Every setting the EA reads, grouped — no JSON."
              />
            ) : schemaError ? (
              // Without the schema the form has no field list, and guessing
              // one would offer settings the EA may not have.
              <EmptyState title="Could not load the template fields" hint={schemaError} />
            ) : !schema ? (
              <EmptyState title="Loading the template fields" />
            ) : schema.length === 0 ? (
              // An empty field list is not an empty template. Saving from one
              // would send `{}`, which `_clean_fields` reads as "every field
              // is a default" -- silently resetting a tuned template.
              <EmptyState
                title="The backend described no template fields"
                hint="Nothing can be edited safely until it does."
              />
            ) : (
              <TemplateEditor
                key={selected ?? ""}
                name={String(current["name"])}
                values={current}
                schema={schema}
                onSave={onSave}
                onClose={() => setSelected(null)}
              />
            )}
          </div>
        </div>
      )}

      {current && (
        <div className="flex shrink-0 justify-end">
          <Button variant="danger" onClick={() => {
            onDelete(String(current["name"]));
            setSelected(null);
          }}>
            Delete {String(current["name"])}
          </Button>
        </div>
      )}
    </div>
  );
}
