import { EmptyState } from "@/components/shared/EmptyState";
import { formatSignedMoney } from "@/components/shared/format";
import type { HourlyCell } from "@/api/types";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const HOURS = Array.from({ length: 24 }, (_, h) => h);

/**
 * When this account makes and loses money, by UTC weekday and hour.
 *
 * Green for profit and red for loss, at an opacity set by size relative to the
 * biggest cell — so the scale is honest about magnitude rather than colouring
 * a $2 hour the same as a $200 one. An hour with no trades is neutral, not
 * green: no data and break-even are different answers.
 */
export function HeatmapSection({ cells }: { cells: HourlyCell[] }) {
  if (cells.length === 0) {
    return (
      <EmptyState
        title="No closed trades in this window"
        hint="The heatmap fills in as trades close."
      />
    );
  }

  const byKey = new Map(cells.map((c) => [`${c.weekday}-${c.hour}`, c]));
  const peak = Math.max(...cells.map((c) => Math.abs(c.pnl)), 1);

  return (
    <div className="overflow-x-auto">
      <table className="border-separate border-spacing-[2px] text-[10px]">
        <thead>
          <tr>
            <th />
            {HOURS.map((h) => (
              <th key={h} className="num w-6 font-normal text-ink-3">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {DAYS.map((label, weekday) => (
            <tr key={label}>
              <th className="pr-1 text-right font-normal text-ink-3">{label}</th>
              {HOURS.map((hour) => {
                const cell = byKey.get(`${weekday}-${hour}`);
                const intensity = cell ? Math.abs(cell.pnl) / peak : 0;
                const colour = !cell || cell.n === 0
                  ? "var(--color-surface-2)"
                  : cell.pnl >= 0
                    ? `color-mix(in oklab, var(--color-profit) ${Math.round(intensity * 85) + 15}%, var(--color-surface-2))`
                    : `color-mix(in oklab, var(--color-loss) ${Math.round(intensity * 85) + 15}%, var(--color-surface-2))`;
                return (
                  <td
                    key={hour}
                    data-testid={cell ? `cell-${weekday}-${hour}` : undefined}
                    title={
                      cell
                        ? `${label} ${String(hour).padStart(2, "0")}:00 UTC (${cell.session}) — ${formatSignedMoney(cell.pnl)} over ${cell.n} trade${cell.n === 1 ? "" : "s"}`
                        : `${label} ${String(hour).padStart(2, "0")}:00 UTC — no trades`
                    }
                    style={{ background: colour }}
                    className="h-5 w-6 rounded-sm"
                  />
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-[11px] text-ink-3">
        UTC. Shade is size relative to the biggest hour; an hour with no trades is blank.
      </p>
    </div>
  );
}
