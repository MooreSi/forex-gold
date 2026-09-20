import { useCallback } from "react";
import { Copy } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney } from "@/components/shared/format";
import { asArray, asObject } from "@/lib/asArray";
import { ScheduleWindowRow, type OverrideChoice } from "./ScheduleWindowRow";
import { TradingClockCard } from "./TradingClockCard";
import { TradingMarketsCard } from "./TradingMarketsCard";

interface ScheduleState {
  schedule: Record<string, unknown>;
  enabled: boolean;
  daily_target: number;
  daily_state: Record<string, unknown>;
  clock: Record<string, unknown>;
  markets?: Record<string, unknown>;
  override_options?: OverrideChoice[];
  channels?: string[];
}

interface ScheduleSectionProps {
  state: ScheduleState | null;
  onSetEnabled: (enabled: boolean) => void;
  onSetSchedule: (schedule: Record<string, unknown>) => void;
  onSetTarget: (target: number) => void;
  onResumeToday: () => void;
  onSetMarket: (market: string, enabled: boolean) => void;
  onSetClockOffset: (minutes: number | null) => void;
}

// The service's own day keys. The grid is stored under these names and a
// mismatch here silently edits a day that is never read back.
const DAYS = [
  "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
];

function blocks(schedule: Record<string, unknown>, day: string): Record<string, unknown>[] {
  return asArray<Record<string, unknown>>(schedule[day]);
}

/**
 * Trading Schedule: when automated orders may be placed, and by whom.
 *
 * Three gates stack here, and they are independent. The **markets** decide
 * which sessions accept a trade at all. The **windows** cap the hours and the
 * profit within a day. The **daily target** stops everything once the day has
 * earned enough, whatever the windows still allow.
 *
 * None of it touches signal generation or Telegram ingestion — this blocks and
 * redirects the final order-placement step only, and only for automated
 * orders. Manual orders are always exempt.
 */
export function ScheduleSection({
  state, onSetEnabled, onSetSchedule, onSetTarget, onResumeToday,
  onSetMarket, onSetClockOffset,
}: ScheduleSectionProps) {
  const setBlock = useCallback(
    (day: string, index: number, patch: Record<string, unknown>) => {
      if (!state) return;
      const next = { ...state.schedule };
      next[day] = blocks(next, day).map((b, i) => (i === index ? { ...b, ...patch } : b));
      onSetSchedule(next);
    },
    [state, onSetSchedule],
  );

  // Monday's windows, copied over every other day. The grid is 7 days x 4
  // windows with a channel list inside each; setting that up by hand is 28
  // identical edits, which is how a schedule ends up subtly inconsistent.
  const copyMondayToAll = useCallback(() => {
    if (!state) return;
    const monday = blocks(state.schedule, "monday");
    if (monday.length === 0) return;
    const next = { ...state.schedule };
    for (const day of DAYS) {
      if (day === "monday") continue;
      next[day] = blocks(next, day).map((b, i) =>
        (monday[i] ? { ...monday[i] } : b));
    }
    onSetSchedule(next);
  }, [state, onSetSchedule]);

  if (!state) return <EmptyState title="Loading the schedule" />;

  const daily = asObject(state.daily_state);
  const reached = daily["reached"] === true;
  const overridden = daily["overridden"] === true;
  const options = asArray<OverrideChoice>(state.override_options);
  const channels = asArray<string>(state.channels);

  return (
    <div className="space-y-4">
      <TradingClockCard clock={state.clock} onSetOffset={onSetClockOffset} />
      <TradingMarketsCard markets={state.markets ?? {}} onSetMarket={onSetMarket} />

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
        <Button variant="ghost" onClick={copyMondayToAll}>
          <Copy className="mr-1 inline h-3 w-3" aria-hidden />
          Copy Monday to all days
        </Button>
      </div>

      {reached && (
        <div
          data-testid="daily-target-reached"
          className="flex flex-wrap items-center gap-2 rounded border border-warning/40 bg-warning/10 px-2 py-1 text-[11px] text-warning"
        >
          Day&apos;s target reached ({formatMoney(Number(daily["pnl"] ?? 0))} of{" "}
          {formatMoney(Number(daily["target"] ?? 0))}) — automated entries are held
          <Button variant="ghost" onClick={onResumeToday}>Resume for today</Button>
        </div>
      )}
      {overridden && (
        <p className="text-[11px] text-ink-3">
          Resumed for today only; this clears at the day boundary.
        </p>
      )}

      <div className="space-y-2">
        {DAYS.map((day) => (
          <section key={day} className="rounded-lg border border-line bg-surface-1 p-2">
            <h4 className="mb-1 text-xs font-semibold capitalize text-accent">{day}</h4>
            <div className="space-y-1">
              {blocks(state.schedule, day).map((block, i) => (
                <ScheduleWindowRow
                  key={i}
                  day={day}
                  dayLabel={day}
                  index={i}
                  block={block}
                  channels={channels}
                  options={options}
                  onPatch={(patch) => setBlock(day, i, patch)}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
