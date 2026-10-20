import { ShieldAlert, Target } from "lucide-react";
import { formatMoney, pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import type { TradeRow } from "../hooks/useClosedTrades";
import { dayStats, goalMarks, intradayCurve, type GoalSettings } from "./calendarStats";

const W = 300, H = 64, PAD = 4;

/**
 * The day's realised P&L close by close, as a step line with its zero
 * (docs/todo/023, #3). Realised only: an open trade's float is not in the
 * closed-trade rows, so this is not an equity curve and does not claim to be.
 */
function IntradayCurveView({ trades }: { trades: TradeRow[] }) {
  const c = intradayCurve(trades);
  const lo = Math.min(0, c.low), hi = Math.max(0, c.peak);
  const span = hi - lo || 1;
  const n = c.points.length - 1 || 1;
  const x = (i: number) => PAD + (i / n) * (W - 2 * PAD);
  const y = (v: number) => PAD + ((hi - v) / span) * (H - 2 * PAD);
  const path = c.points.map((p, i) =>
    i === 0 ? `M${x(0)},${y(p.cum)}` : `H${x(i)}V${y(p.cum)}`).join("");
  const last = c.points[c.points.length - 1]!.cum;
  return (
    <svg data-testid="intraday-curve" viewBox={`0 0 ${W} ${H}`} className="h-16 w-full"
      role="img" aria-label="Realised P&L through the day">
      <line x1={PAD} x2={W - PAD} y1={y(0)} y2={y(0)}
        className="stroke-line" strokeDasharray="3 3" />
      <path d={path} fill="none" strokeWidth={1.5}
        className={last >= 0 ? "stroke-profit" : "stroke-loss"} />
    </svg>
  );
}

/**
 * The day's numbers beyond net and win rate (docs/todo/023, #2, #3, #7, #10):
 * profit factor, averages, best and worst, fees, realised drawdown, and
 * whether it reached the daily goal or the loss limit.
 */
export function DayStatsSection({ trades, dayOpen, risk }: {
  trades: TradeRow[]; dayOpen: number | null; risk: GoalSettings;
}) {
  const s = dayStats(trades);
  const curve = intradayCurve(trades);
  const marks = goalMarks(trades, dayOpen, risk);
  const money = (v: number | null) => (v == null ? "—" : formatMoney(v));
  const cells: [string, string, string, string?][] = [
    ["Profit factor", s.profitFactor == null ? "—" : s.profitFactor.toFixed(2), "stat-pf"],
    ["Avg win", money(s.avgWin), "stat-avg-win", "text-profit"],
    ["Avg loss", money(s.avgLoss), "stat-avg-loss", "text-loss"],
    ["Best", money(s.best), "stat-best", pnlColour(s.best)],
    ["Worst", money(s.worst), "stat-worst", pnlColour(s.worst)],
    ["Fees", formatMoney(s.fees), "stat-fees"],
    ["Peak", formatMoney(curve.peak), "stat-peak", pnlColour(curve.peak)],
    ["Max drawdown", formatMoney(-curve.maxDrawdown), "stat-max-dd",
      curve.maxDrawdown > 0 ? "text-loss" : undefined],
  ];
  return (
    <section className="space-y-2">
      <div className="grid grid-cols-4 gap-1.5">
        {cells.map(([label, value, testId, tone]) => (
          <div key={testId} data-testid={testId}
            className="rounded border border-line bg-surface-2/40 px-2 py-1">
            <div className="text-[10px] uppercase tracking-wider text-ink-3">{label}</div>
            <div className={cn("num font-semibold text-ink-1", tone)}>{value}</div>
          </div>
        ))}
      </div>
      <IntradayCurveView trades={trades} />
      {(marks.goal || marks.loss) && (
        <div className="flex gap-3 text-[11px]">
          {marks.goal && (
            <span data-testid="day-goal-reached" className="flex items-center gap-1 text-profit">
              <Target size={12} /> Daily goal reached
            </span>
          )}
          {marks.loss && (
            <span data-testid="day-loss-reached" className="flex items-center gap-1 text-loss">
              <ShieldAlert size={12} /> Daily loss limit reached
            </span>
          )}
        </div>
      )}
    </section>
  );
}
