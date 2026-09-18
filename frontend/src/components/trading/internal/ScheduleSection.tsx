import { useCallback } from "react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney } from "@/components/shared/format";
import { asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

interface ScheduleState {
  schedule: Record<string, unknown>;
  enabled: boolean;
  daily_target: number;
  daily_state: Record<string, unknown>;
  clock: Record<string, unknown>;
}

interface ScheduleSectionProps {
  state: ScheduleState | null;
  onSetEnabled: (enabled: boolean) => void;
  onSetSchedule: (schedule: Record<string, unknown>) => void;
  onSetTarget: (target: number) => void;
  onResumeToday: () => void;
}

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABEL: Record<string, string> = {
  mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun",
};

function blocks(schedule: Record<string, unknown>, day: string): Record<string, unknown>[] {
  const raw = schedule[day];
  return Array.isArray(raw) ? (raw as Record<string, unknown>[]) : [];
}

/**
 * Per-day trading windows, and the whole-day profit target.
 *
 * The clock the windows are measured in is stated at the top. It was an open
 * question once (simon-handover/017) and a schedule screen that does not
 * answer it is describing hours in an unknown timezone.
 */
export function ScheduleSection({
  state, onSetEnabled, onSetSchedule, onSetTarget, onResumeToday,
}: ScheduleSectionProps) {
  const setBlock = useCallback(
    (day: string, index: number, patch: Record<string, unknown>) => {
      if (!state) return;
      const next = { ...state.schedule };
      const rows = blocks(next, day).map((b, i) => (i === index ? { ...b, ...patch } : b));
      next[day] = rows;
      onSetSchedule(next);
    },
    [state, onSetSchedule],
  );

  if (!state) return <EmptyState title="Loading the schedule" />;

  const daily = asObject(state.daily_state);
  const reached = daily["reached"] === true;
  const overridden = daily["overridden"] === true;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-ink-2">
          <input
            type="checkbox"
            aria-label="Only trade inside these windows"
            checked={state.enabled}
            onChange={(e) => onSetEnabled(e.target.checked)}
            className="accent-accent"
          />
          Only trade inside these windows
        </label>
        <span className="text-[11px] text-ink-3">
          Times are {String(asObject(state.clock)["label"] ?? "in an unknown clock")}
        </span>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-ink-2">
          Whole-day profit target
          <input
            aria-label="Whole-day profit target"
            inputMode="decimal"
            defaultValue={String(state.daily_target)}
            onBlur={(e) => onSetTarget(Number(e.target.value) || 0)}
            className="num ml-2 w-24 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
          />
          <span className="ml-1 text-[11px] text-ink-3">0 turns this gate off</span>
        </label>
        {reached && (
          <span
            data-testid="daily-target-reached"
            className="flex items-center gap-2 rounded border border-warning/40 bg-warning/10 px-2 py-1 text-[11px] text-warning"
          >
            Day's target reached ({formatMoney(Number(daily["pnl"] ?? 0))} of{" "}
            {formatMoney(Number(daily["target"] ?? 0))}) — automated entries are held
            <Button variant="ghost" onClick={onResumeToday}>Resume for today</Button>
          </span>
        )}
        {overridden && (
          <span className="text-[11px] text-ink-3">
            Resumed for today only; this clears at the day boundary.
          </span>
        )}
      </div>

      <div className="space-y-1.5">
        {DAYS.map((day) => (
          <div key={day} className="flex flex-wrap items-center gap-2">
            <span className="num w-10 text-[11px] text-ink-3">{DAY_LABEL[day]}</span>
            {blocks(state.schedule, day).map((block, i) => (
              <span
                key={i}
                data-testid={`window-${day}-${i}`}
                className={cn(
                  "flex items-center gap-1 rounded border px-2 py-1",
                  block["enabled"] ? "border-line bg-surface-2" : "border-line/50 bg-surface-1",
                )}
              >
                <input
                  type="checkbox"
                  aria-label={`${DAY_LABEL[day]} window ${i + 1}`}
                  checked={Boolean(block["enabled"])}
                  onChange={(e) => setBlock(day, i, { enabled: e.target.checked })}
                  className="accent-accent"
                />
                <input
                  aria-label={`${DAY_LABEL[day]} window ${i + 1} start`}
                  defaultValue={String(block["start"] ?? "")}
                  onBlur={(e) => setBlock(day, i, { start: e.target.value })}
                  className="num w-12 bg-transparent text-[11px] text-ink-1"
                />
                <span className="text-ink-3">–</span>
                <input
                  aria-label={`${DAY_LABEL[day]} window ${i + 1} end`}
                  defaultValue={String(block["end"] ?? "")}
                  onBlur={(e) => setBlock(day, i, { end: e.target.value })}
                  className="num w-12 bg-transparent text-[11px] text-ink-1"
                />
              </span>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
