import { useEffect, useState } from "react";

interface SettingsFieldProps {
  label: string;
  value: string;
  hint?: string;
  type?: "text" | "password" | "number";
  /** Bumped by the owning resource after every save; see useSettingsResource. */
  version?: number;
  onCommit: (value: string) => void;
}

/**
 * One field that saves when it is left, not on every keystroke.
 *
 * Held as a string while being edited: a field whose state is a number cannot
 * hold a half-typed decimal, and `Number("1.")` is 1 — so "1.25" arrives as
 * 125. That happened on the Backtest form and is the reason this component
 * exists rather than each tab rolling its own input.
 */
export function SettingsField({
  label, value, hint, type = "text", version = 0, onCommit,
}: SettingsFieldProps) {
  const [draft, setDraft] = useState(value);

  // The stored value wins after every save, not only when it CHANGES.
  //
  // "Rejected your number" and "agreed with what was stored" look identical
  // from here — both leave `value` where it was — so a field keyed on `value`
  // alone keeps the rejected input on screen. Typing 99 into a risk field the
  // service clamps back to 2 left "99" in the box, which reads as a 99% risk
  // setting the engine is not using.
  useEffect(() => setDraft(value), [value, version]);

  return (
    <label className="block text-xs text-ink-2">
      {label}
      <input
        aria-label={label}
        type={type === "password" ? "password" : "text"}
        inputMode={type === "number" ? "decimal" : undefined}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => draft !== value && onCommit(draft)}
        className="num mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
      />
      {hint && <span className="mt-0.5 block text-[10px] text-ink-3">{hint}</span>}
    </label>
  );
}
