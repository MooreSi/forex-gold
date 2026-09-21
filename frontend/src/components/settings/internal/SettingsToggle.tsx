import { Tooltip } from "@/components/shared/Tooltip";

interface SettingsToggleProps {
  label: string;
  checked: boolean;
  hint?: string;
  onChange: (checked: boolean) => void;
}

/**
 * A stored on/off, saved the moment it is flicked.
 *
 * Unlike {@link SettingsField} there is no draft state and no blur: a checkbox
 * has no half-typed value to protect, and waiting for a blur that a checkbox
 * never really gets would leave the operator looking at a switch that says one
 * thing while the engine does another.
 */
export function SettingsToggle({ label, checked, hint, onChange }: SettingsToggleProps) {
  return (
    <label className="flex items-start gap-2 text-xs text-ink-2">
      <Tooltip label={hint}>
        <input
          type="checkbox"
          checked={checked}
          aria-label={label}
          onChange={(e) => onChange(e.target.checked)}
          className="accent-accent mt-0.5"
        />
      </Tooltip>
      <span>
        {label}
        {hint && <span className="mt-0.5 block text-[10px] text-ink-3">{hint}</span>}
      </span>
    </label>
  );
}
