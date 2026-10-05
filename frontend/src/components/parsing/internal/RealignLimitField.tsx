import { useEffect, useState } from "react";
import { Tooltip } from "@/components/shared/Tooltip";

interface RealignLimitFieldProps {
  /** The stored limit in pips; 0, null or absent mean no limit. */
  value: unknown;
  onSave: (pips: number) => Promise<void>;
}

/**
 * Entry Realignment's "realign up to N pips" (owner, 2026-10-02).
 *
 * One limit, both ways: price that MISSED the zone (a BUY above it, a SELL
 * below) and price that went THROUGH it toward the stop are both realigned
 * within N pips. Missed by more waits for the zone; breached by more is
 * discarded. Blank is no limit and changes nothing from before the field
 * existed, which is why a stored 0 is shown as an empty box rather than "0":
 * "0 pips" would read as "never realign".
 *
 * Saved when you leave the field, like the other numeric settings on this
 * page. A cleared box saves 0.
 */
export function RealignLimitField({ value, onSave }: RealignLimitFieldProps) {
  const [text, setText] = useState("");

  useEffect(() => {
    const n = Number(value);
    setText(Number.isFinite(n) && n > 0 ? String(n) : "");
  }, [value]);

  return (
    <div className="mt-2 flex items-center gap-1.5 text-[11px] text-ink-2">
      <label htmlFor="realign-limit-pips" className="font-semibold text-ink-1">
        Realign up to
      </label>
      <Tooltip label="The furthest, in pips, a signal may be chased into a market entry: price that ran away from the zone and price that went through it toward the stop. Past it the signal waits for the zone (ran away) or is discarded (breached). Blank is no limit.">
        <input
          id="realign-limit-pips"
          aria-label="Realign up to"
          inputMode="decimal"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => {
            const n = Number(text);
            void onSave(Number.isFinite(n) && n > 0 ? n : 0);
          }}
          className="num w-16 rounded border border-line bg-surface-1 px-2 py-0.5 text-right
                     text-ink-1 focus:border-accent focus:outline-none"
        />
      </Tooltip>
      <span className="text-ink-3">pips (1 pip = 0.10)</span>
    </div>
  );
}
