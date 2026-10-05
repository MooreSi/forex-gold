import { LADDER_FIELD } from "../content/templateGroups";
import { TemplateField, type SchemaField } from "./TemplateField";

interface TemplateLadderGridProps {
  /** "tp" for the anchor ladder, "tp_pen" for the pending one. */
  prefix: "tp" | "tp_pen";
  fields: SchemaField[];
  draft: Record<string, string | boolean>;
  onChange: (name: string, v: string | boolean) => void;
}

/**
 * A TP ladder as one card per level: the level's pips, and directly under it
 * the Take % closed there.
 *
 * The owner, 2026-10-01: "under each tp1, tp2 pips directly under have the box
 * called Take % so its easier to identify and modify, also make each box less
 * wide so you fit 4 in a row". The flat grid put all the pips boxes first and
 * all the % boxes after them, labelled "tp1 pct", so the two numbers that
 * describe one level were half a screen apart.
 *
 * Anything in the group that is not a level box (the "use the signal's own
 * targets" switch) is rendered above the cards by the editor, not here.
 */
export function TemplateLadderGrid({ prefix, fields, draft, onChange }: TemplateLadderGridProps) {
  const levels = new Map<number, { pips?: SchemaField; pct?: SchemaField }>();
  for (const f of fields) {
    const m = LADDER_FIELD.exec(f.name);
    if (!m || m[1] !== prefix) continue;
    const n = Number(m[2]);
    const slot = levels.get(n) ?? {};
    slot[m[3] === "pips" ? "pips" : "pct"] = f;
    levels.set(n, slot);
  }
  const ordered = [...levels.entries()].sort(([a], [b]) => a - b);

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {ordered.map(([n, { pips, pct }]) => (
        <div
          key={n}
          data-testid={`ladder-${prefix}-${n}`}
          className="rounded border border-line bg-surface-2 px-2 py-1"
        >
          <p className="text-[11px] font-semibold text-ink-1">TP{n}</p>
          {pips && (
            <TemplateField
              field={pips}
              caption="Pips"
              value={draft[pips.name]}
              onChange={(v) => onChange(pips.name, v)}
            />
          )}
          {pct && (
            <TemplateField
              field={pct}
              caption="Take %"
              value={draft[pct.name]}
              onChange={(v) => onChange(pct.name, v)}
            />
          )}
        </div>
      ))}
    </div>
  );
}
