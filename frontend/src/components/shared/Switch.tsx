import { Tooltip } from "./Tooltip";
import { cn } from "@/lib/cn";

interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  /** The accessible name. Tests and screen readers find the control by it. */
  label: string;
  /** Hover help. Required: a switch nobody can explain is one nobody dares touch. */
  help: string;
  disabled?: boolean;
}

/**
 * An on/off switch. A real checkbox underneath, so it is keyboard- and
 * screen-reader-native and `toBeChecked()` reads it, drawn as a track and
 * knob so on and off read at a glance across a grid of them.
 */
export function Switch({ checked, onChange, label, help, disabled }: SwitchProps) {
  return (
    <Tooltip label={help}>
      <span className="relative inline-flex shrink-0 items-center">
        <input
          type="checkbox"
          role="switch"
          aria-label={label}
          checked={checked}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
          className="peer absolute inset-0 z-10 m-0 cursor-pointer opacity-0 disabled:cursor-not-allowed"
        />
        <span
          aria-hidden
          className={cn(
            "h-4 w-7 rounded-full border transition-colors",
            "peer-focus-visible:ring-2 peer-focus-visible:ring-accent/60",
            checked ? "border-profit/60 bg-profit/30" : "border-line bg-surface-3",
            disabled && "opacity-40",
          )}
        />
        <span
          aria-hidden
          className={cn(
            "pointer-events-none absolute top-0.5 size-3 rounded-full transition-all",
            checked ? "left-3.5 bg-profit" : "left-0.5 bg-ink-3",
          )}
        />
      </span>
    </Tooltip>
  );
}
