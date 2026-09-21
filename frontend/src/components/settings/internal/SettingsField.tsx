import { useEffect, useRef, useState } from "react";
import { Tooltip } from "@/components/shared/Tooltip";

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
  // Whether the operator is in this field right now. A ref, not state: it must
  // not re-run the effect below, only decide whether that effect may write.
  const editing = useRef(false);

  // The stored value wins after every save, not only when it CHANGES.
  //
  // "Rejected your number" and "agreed with what was stored" look identical
  // from here — both leave `value` where it was — so a field keyed on `value`
  // alone keeps the rejected input on screen. Typing 99 into a risk field the
  // service clamps back to 2 left "99" in the box, which reads as a 99% risk
  // setting the engine is not using.
  //
  // **Except while the field is focused**, because this used to overwrite what
  // was being typed. The settings read is asynchronous, so opening a tab and
  // starting to edit before it resolves put the stored number back into the
  // box mid-edit. It surfaced on CI as a flake rather than a report: the load
  // landed between a `clear()` and a `type("2")`, the field still held "1",
  // took the "2" on the end, and saved 12% risk per trade where 2 was asked
  // for. The same race on a real machine writes the same number.
  //
  // Losing focus is deliberately NOT a dependency here. A blur commits, and
  // re-syncing at that moment would snap the field back to the stale stored
  // value for as long as the save takes. The bumped `version` that follows the
  // save is what brings the field back into line, by which time `editing` is
  // false.
  useEffect(() => {
    if (editing.current) return;
    setDraft(value);
  }, [value, version]);

  return (
    <label className="block text-xs text-ink-2">
      {label}
      {/* The hint is BOTH printed underneath and shown on hover. Underneath is
          where somebody reading the form finds it; on hover is where somebody
          who has already started typing in the box finds it, which is the
          moment they actually want it (owner request, 2026-09-21). */}
      <Tooltip label={hint}>
      <input
        aria-label={label}
        type={type === "password" ? "password" : "text"}
        inputMode={type === "number" ? "decimal" : undefined}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onFocus={() => { editing.current = true; }}
        onBlur={() => {
          editing.current = false;
          if (draft !== value) onCommit(draft);
        }}
        className="num mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
      />
      </Tooltip>
      {hint && <span className="mt-0.5 block text-[10px] text-ink-3">{hint}</span>}
    </label>
  );
}
