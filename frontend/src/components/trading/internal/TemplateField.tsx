import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import { FIELD_HINTS, FIELD_UNITS, labelFor } from "../content/templateGroups";
import { helpFor } from "../content/templateHelp";

export interface SchemaField {
  name: string;
  type: string;
  default: unknown;
  choices: string[];
}

interface TemplateFieldProps {
  field: SchemaField;
  value: string | boolean;
  onChange: (v: string | boolean) => void;
  /** Why this field sizes nothing right now, when it does not. */
  overriddenBy?: string;
  /** The text shown above the box. Defaults to the field's full label; the
   *  ladder grid passes "Pips"/"Take %" because its card already names the
   *  level. The accessible name is always the full label. */
  caption?: string;
}

/**
 * One EA template setting: its label, the control its declared type calls
 * for, its unit, and a hover explanation of what it does (templateHelp.ts).
 *
 * Each control carries its own tooltip (a switch's covers its label too),
 * one per control, which is what the repo-wide hover-help guard counts. The
 * short hint that some fields carry (FIELD_HINTS: "0 is off.") stays on screen under the box,
 * because those are the ones a wrong guess costs money on.
 */
export function TemplateField({ field, value, onChange, overriddenBy, caption }: TemplateFieldProps) {
  const id = `tpl-${field.name}`;
  const label = labelFor(field.name);
  const unit = FIELD_UNITS[field.name];
  const hint = overriddenBy ?? FIELD_HINTS[field.name];
  const help = helpFor(field.name);

  if (field.type === "boolean") {
    return (
      <Tooltip label={help}>
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
            <span className="text-xs text-ink-1">{caption ?? label}</span>
            {hint && <span className="block text-[10px] text-ink-3">{hint}</span>}
          </span>
        </label>
      </Tooltip>
    );
  }

  return (
    <div className={cn("py-1", overriddenBy && "opacity-60")} data-overridden={overriddenBy ? "true" : undefined}>
      <label htmlFor={id} className="block text-[11px] text-ink-2">{caption ?? label}</label>
      <div className="mt-0.5 flex items-center gap-1.5">
        {field.type === "choice" ? (
          <Tooltip label={help}>
            <select
              id={id}
              aria-label={label}
              value={String(value)}
              onChange={(e) => onChange(e.target.value)}
              className="w-full rounded border border-line bg-surface-1 px-2 py-1 text-xs text-ink-1"
            >
              {field.choices.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </Tooltip>
        ) : (
          <Tooltip label={help}>
            <input
              id={id}
              aria-label={label}
              inputMode="decimal"
              disabled={Boolean(overriddenBy)}
              value={String(value)}
              onChange={(e) => onChange(e.target.value)}
              className="num w-full rounded border border-line bg-surface-1 px-2 py-1 text-xs text-ink-1"
            />
          </Tooltip>
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
