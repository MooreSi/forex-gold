import { useMemo, useState } from "react";
import { Button } from "@/components/shared/Button";
import { iconFor } from "@/components/shared/icons";
import { cn } from "@/lib/cn";
import {
  FIELD_HINTS, FIELD_GROUPS, FIELD_UNITS, OTHER_GROUP_ID, labelFor,
} from "../content/templateGroups";

export interface SchemaField {
  name: string;
  type: string;
  default: unknown;
  choices: string[];
}

interface TemplateEditorProps {
  name: string;
  values: Record<string, unknown>;
  schema: SchemaField[];
  onSave: (name: string, values: Record<string, unknown>) => Promise<{ pushed: boolean }>;
  onClose: () => void;
}

/**
 * The EA template form.
 *
 * It replaced a ten-row textarea holding the template as raw JSON — around a
 * hundred keys, unlabelled, ungrouped, with no indication of which fields are
 * booleans or which are enums with a fixed set of values. The owner's report
 * on 2026-09-19 was simply "i'm unable to edit a template", which was fair.
 *
 * Three rules hold this together:
 *
 * **Every field the backend accepts is reachable.** The field list comes from
 * `GET /api/trading/templates/schema`, and a field no group claims lands in
 * "Other settings" rather than vanishing — so a field added to the backend is
 * editable before anybody edits this file.
 *
 * **A field's declared type decides its control.** A boolean gets a switch, a
 * choice gets its own allowed values, and a number gets a numeric box. The
 * textarea let you save `trail_mode: "stepp"` and find out from a 500.
 *
 * **A save sends every field.** A partial save reaches `_clean_fields` as
 * "the rest are defaults", which silently resets a tuned template.
 *
 * Numbers are held as STRINGS while editing, for the reason the backtest form
 * documents: `Number("1.")` is 1, so re-rendering from the number drops the
 * decimal point the operator just typed.
 */
function initialDraft(values: Record<string, unknown>, schema: SchemaField[]) {
  const draft: Record<string, string | boolean> = {};
  for (const field of schema) {
    const current = values[field.name] ?? field.default;
    draft[field.name] = field.type === "boolean"
      ? Boolean(current)
      : String(current ?? "");
  }
  return draft;
}

function coerce(field: SchemaField, raw: string | boolean): unknown {
  if (field.type === "boolean") return Boolean(raw);
  if (field.type === "choice" || field.type === "text") return String(raw);
  const n = Number(raw);
  // An empty or half-typed box falls back to the schema's own default rather
  // than reaching the service as NaN.
  if (raw === "" || !Number.isFinite(n)) return field.default;
  return field.type === "integer" ? Math.round(n) : n;
}

function Field({ field, value, onChange }: {
  field: SchemaField;
  value: string | boolean;
  onChange: (v: string | boolean) => void;
}) {
  const id = `tpl-${field.name}`;
  const label = labelFor(field.name);
  const unit = FIELD_UNITS[field.name];
  const hint = FIELD_HINTS[field.name];

  if (field.type === "boolean") {
    return (
      <label className="flex items-start gap-2 py-1">
        <input
          id={id}
          type="checkbox"
          aria-label={label}
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
          className="mt-0.5 size-3.5 accent-[var(--color-accent)]"
        />
        <span>
          <span className="text-xs text-ink-1">{label}</span>
          {hint && <span className="block text-[10px] text-ink-3">{hint}</span>}
        </span>
      </label>
    );
  }

  return (
    <div className="py-1">
      <label htmlFor={id} className="block text-[11px] text-ink-2">{label}</label>
      <div className="mt-0.5 flex items-center gap-1.5">
        {field.type === "choice" ? (
          <select
            id={id}
            aria-label={label}
            value={String(value)}
            onChange={(e) => onChange(e.target.value)}
            className="w-full rounded border border-line bg-surface-1 px-2 py-1 text-xs text-ink-1"
          >
            {field.choices.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        ) : (
          <input
            id={id}
            aria-label={label}
            inputMode="decimal"
            value={String(value)}
            onChange={(e) => onChange(e.target.value)}
            className="num w-full rounded border border-line bg-surface-1 px-2 py-1 text-xs text-ink-1"
          />
        )}
        {unit && (
          <span data-testid={`unit-${field.name}`} className="shrink-0 text-[10px] text-ink-3">
            {unit}
          </span>
        )}
      </div>
      {hint && <p className="mt-0.5 text-[10px] text-ink-3">{hint}</p>}
    </div>
  );
}

export function TemplateEditor({
  name, values, schema, onSave, onClose,
}: TemplateEditorProps) {
  const [draft, setDraft] = useState(() => initialDraft(values, schema));
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const groups = useMemo(() => {
    const byName = new Map(schema.map((f) => [f.name, f]));
    const taken = new Set<string>();
    const out: { id: string; title: string; blurb: string; icon: string;
                 fields: SchemaField[] }[] = [];

    for (const group of FIELD_GROUPS) {
      const names = group.fields
        ? group.fields.filter((n) => byName.has(n))
        : schema.filter((f) => group.match?.(f.name)).map((f) => f.name);
      const fields = names.map((n) => byName.get(n)!).filter(Boolean);
      fields.forEach((f) => taken.add(f.name));
      if (fields.length) out.push({ ...group, fields });
    }

    // Anything no group claimed. This is the rule, not a fallback: a field
    // added to the backend must be editable before anybody edits the groups.
    const leftovers = schema.filter((f) => !taken.has(f.name));
    if (leftovers.length) {
      out.push({
        id: OTHER_GROUP_ID, title: "Other settings", icon: "tunables",
        blurb: "Fields with no group of their own yet. They save like any other.",
        fields: leftovers,
      });
    }
    return out;
  }, [schema]);

  const matches = (field: SchemaField) => {
    const q = search.trim().toLowerCase();
    if (!q) return true;
    return field.name.toLowerCase().includes(q)
      || labelFor(field.name).toLowerCase().includes(q);
  };

  const visible = groups
    .map((g) => ({ ...g, fields: g.fields.filter(matches) }))
    .filter((g) => g.fields.length > 0);

  async function save() {
    setBusy(true);
    setResult(null);
    try {
      const payload: Record<string, unknown> = {};
      for (const field of schema) {
        payload[field.name] = coerce(field, draft[field.name]);
      }
      const { pushed } = await onSave(name, payload);
      setResult(pushed
        ? "Saved and pushed to the connected EA."
        : "Saved. No EA is connected, so these apply on the next signal.");
    } catch (e) {
      setResult(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-0 flex-col rounded-lg border border-line bg-surface-1">
      <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <h4 className="text-sm font-semibold text-ink-1">{name}</h4>
        <input
          aria-label="Search settings"
          placeholder="Search settings"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="ml-auto w-44 rounded border border-line bg-surface-2 px-2 py-1 text-[11px] text-ink-1"
        />
        <Button onClick={() => void save()} disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </Button>
        <Button variant="ghost" onClick={onClose}>Close</Button>
      </header>

      {result && (
        <p role="status" className="shrink-0 border-b border-line px-3 py-1.5 text-[11px] text-ink-2">
          {result}
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-auto p-3">
        {visible.length === 0 ? (
          <p className="text-xs text-ink-3">No setting matches “{search}”.</p>
        ) : (
          <div className="space-y-4">
            {visible.map((group) => {
              const Icon = iconFor(group.icon);
              return (
                <section key={group.id}>
                  <h5 className="flex items-center gap-1.5 text-xs font-semibold text-ink-1">
                    {Icon && <Icon size={13} className="text-accent" aria-hidden />}
                    {group.title}
                  </h5>
                  <p className="mb-1.5 text-[10px] text-ink-3">{group.blurb}</p>
                  <div className={cn(
                    "grid gap-x-4 gap-y-0.5",
                    "sm:grid-cols-2 lg:grid-cols-3",
                  )}>
                    {group.fields.map((field) => (
                      <Field
                        key={field.name}
                        field={field}
                        value={draft[field.name]}
                        onChange={(v) => setDraft((d) => ({ ...d, [field.name]: v }))}
                      />
                    ))}
                  </div>
                </section>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
