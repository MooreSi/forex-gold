import { useState, type ReactNode } from "react";
import { Switch } from "@/components/shared/Switch";
import { cn } from "@/lib/cn";
import type { ParsingToggle } from "../content/settings";

interface ParsingToggleCardProps {
  toggle: ParsingToggle;
  on: boolean;
  /** Set for a switch that can refuse trades, so on reads as a warning. */
  blocks?: boolean;
  onChange: (on: boolean) => void;
  /** A control that belongs to this switch (a limit on what it does). */
  children?: ReactNode;
}

/** Above this many characters the description starts folded to two lines. */
const FOLD_AT = 150;

/**
 * One parsing switch: its name, its state, and what it does.
 *
 * The description is always in the page, never only in a tooltip. Folding a
 * long one to two lines keeps a grid of fifteen readable, and "More" opens it
 * in place. A switch whose explanation had to be hunted for is one somebody
 * turns on without reading.
 */
export function ParsingToggleCard({ toggle, on, blocks, onChange, children }: ParsingToggleCardProps) {
  const long = toggle.description.length > FOLD_AT;
  const [open, setOpen] = useState(false);

  return (
    <div
      data-testid={`toggle-${toggle.key}`}
      className={cn(
        "flex h-full flex-col rounded-md border bg-surface-2 p-3 transition-colors",
        on ? (blocks ? "border-warning/50" : "border-profit/40") : "border-line",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <span className="text-xs font-semibold text-ink-1">{toggle.label}</span>
        <Switch
          checked={on}
          onChange={onChange}
          label={toggle.label}
          help={toggle.description}
        />
      </div>
      <p
        className={cn(
          "mt-1.5 text-[11px] leading-snug text-ink-3",
          long && !open && "line-clamp-2",
        )}
      >
        {toggle.description}
      </p>
      {long && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="mt-1 self-start text-[11px] font-medium text-accent hover:underline"
        >
          {open ? "Less" : "More"}
        </button>
      )}
      {children}
    </div>
  );
}
