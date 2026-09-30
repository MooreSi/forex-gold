import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import { Tooltip } from "@/components/shared/Tooltip";
import { SettingsField } from "@/components/settings/internal/SettingsField";
import { SettingsToggle } from "@/components/settings/internal/SettingsToggle";

interface DailyGoalSectionProps {
  data: Record<string, unknown>;
  version: number;
  save: (body: Record<string, unknown>) => Promise<void>;
}

type Mode = "pct" | "usd";

/** Slider range per mode. A value typed beyond it widens the slider. */
const RANGE: Record<Mode, { min: number; max: number; step: number; fresh: number }> = {
  pct: { min: 0.25, max: 10, step: 0.25, fresh: 1 },
  usd: { min: 10, max: 2000, step: 10, fresh: 100 },
};

const num = (v: unknown) => {
  const n = Number(v ?? 0);
  return Number.isFinite(n) ? n : 0;
};

const describe = (mode: Mode, value: number) =>
  mode === "usd" ? `$${value.toLocaleString()}` : `${value}% of the day's opening balance`;

/**
 * Risk > Stopping for the day > Daily goal (owner, 2026-09-29,
 * docs/todo/risk/020).
 *
 * Once today's REALISED profit reaches the goal, new entries stop until the
 * next broker day; trades already open run on to their own stop and target.
 * A percentage is of the day's opening balance, so the same 1% asks for more
 * as the account grows -- that is the compounding the owner asked for.
 *
 * The slider saves on release, not on every step: each save is a settings
 * write that is also proposed to the paired node, and dragging across twenty
 * steps is one decision.
 */
export function DailyGoalSection({ data, version, save }: DailyGoalSectionProps) {
  const enabled = Boolean(num(data.daily_goal_enabled));
  const protectBe = Boolean(num(data.daily_goal_protect_be));
  const mode: Mode = data.daily_goal_mode === "usd" ? "usd" : "pct";
  const stored = num(data.daily_goal_value);
  const range = RANGE[mode];
  const [draft, setDraft] = useState(stored);
  useEffect(() => setDraft(stored), [stored, version]);

  const commit = () => {
    if (draft !== stored) void save({ daily_goal_value: draft });
  };
  const choose = (next: Mode) => {
    if (next === mode) return;
    void save({ daily_goal_mode: next, daily_goal_value: RANGE[next].fresh });
  };

  return (
    <div data-testid="daily-goal" className="space-y-3 rounded border border-line p-3">
      <div data-testid="risk-daily_goal_enabled">
        <SettingsToggle
          label="Stop for the day once the goal is secured"
          hint="Counts realised profit since the broker day opened. Open trades are left to run; only new entries stop. The goal is secured once they have all closed: if their losses take the day back under it, trading resumes. Resume starts a fresh goal from that moment."
          checked={enabled}
          onChange={(v) => void save({ daily_goal_enabled: v ? 1 : 0 })}
        />
      </div>

      <div className={cn("space-y-3", !enabled && "opacity-60")}>
        <div data-testid="risk-daily_goal_mode" className="flex flex-wrap items-center gap-3">
          <span id="daily-goal-mode-label" className="text-xs text-ink-2">Goal in</span>
          <div role="radiogroup" aria-labelledby="daily-goal-mode-label"
            className="inline-flex overflow-hidden rounded border border-line">
            {(["pct", "usd"] as const).map((m) => (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={mode === m}
                aria-label={m === "pct" ? "%" : "$"}
                onClick={() => choose(m)}
                className={cn(
                  "px-3 py-1 text-xs transition-colors",
                  mode === m ? "bg-accent/15 font-semibold text-ink-1"
                    : "bg-surface-1 text-ink-3 hover:bg-surface-2 hover:text-ink-2",
                )}
              >
                {m === "pct" ? "%" : "$"}
              </button>
            ))}
          </div>
          <Tooltip label="Once the goal is reached, each open trade's stop moves to entry plus costs as soon as price gives it room, so open trades cannot take the day back under the goal.">
            <label className="inline-flex items-center gap-1.5 text-xs text-ink-2">
              <input
                type="checkbox"
                data-testid="risk-daily_goal_protect_be"
                checked={protectBe}
                onChange={(e) => void save({ daily_goal_protect_be: e.target.checked ? 1 : 0 })}
                className="accent-accent"
              />
              Move stops to breakeven once reached
            </label>
          </Tooltip>
        </div>

        <div data-testid="risk-daily_goal_value" className="grid items-center gap-3 sm:grid-cols-[1fr_8rem]">
          <Tooltip label="Drag to set the goal; it is saved when you let go.">
          <input
            type="range"
            aria-label="Daily goal"
            min={range.min}
            max={Math.max(range.max, stored)}
            step={range.step}
            value={draft}
            onChange={(e) => setDraft(Number(e.target.value))}
            onPointerUp={commit}
            onKeyUp={commit}
            onBlur={commit}
            className="accent-accent w-full"
          />
          </Tooltip>
          <SettingsField
            label={mode === "usd" ? "Goal ($)" : "Goal (%)"}
            type="number"
            version={version}
            value={String(stored)}
            onCommit={(v) => void save({ daily_goal_value: Number(v) })}
          />
        </div>

        <p data-testid="daily-goal-summary" className="text-[11px] text-ink-1">
          {enabled
            ? `New entries stop for the day once realised profit reaches ${describe(mode, draft)}.`
            : "Off. Trading does not stop for profit."}
        </p>
        <p className="text-[10px] text-ink-3">
          Checked every few seconds, so a trade that arrives in the seconds after the goal
          is reached can still open. Separate from the $ target on Trading &gt; Schedule,
          which only applies while the schedule is switched on.
        </p>
      </div>
    </div>
  );
}
