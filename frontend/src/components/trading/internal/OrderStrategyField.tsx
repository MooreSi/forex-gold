import { Tooltip } from "@/components/shared/Tooltip";
import type { StrategyChoice } from "./orderStrategy";

/**
 * The Strategy choice both order dialogs share. Blank is a single take
 * profit; see `orderStrategy.ts`.
 */
export function OrderStrategyField({
  value, onChange, choices, note,
}: {
  value: string;
  onChange: (v: string) => void;
  choices: StrategyChoice[];
  note?: string;
}) {
  const hint = "Leave blank to close the whole position at the take profit: no partials, breakeven or trailing.";
  return (
    <label className="block">
      <span className="text-xs text-ink-2">Strategy</span>
      <Tooltip label={hint}>
        <select
          aria-label="Strategy"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="mt-1 w-full rounded border border-line bg-surface-1 px-2 py-1.5 text-sm text-ink-1"
        >
          <option value="">None — single take profit (100% at TP)</option>
          {choices.map((s) => (
            <option key={s.key} value={s.key}>{s.label}</option>
          ))}
        </select>
      </Tooltip>
      <span className="mt-1 block text-[11px] text-ink-3">{hint}{note ? ` ${note}` : ""}</span>
    </label>
  );
}
