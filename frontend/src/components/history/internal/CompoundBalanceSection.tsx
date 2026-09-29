import { useMemo, useState, type MouseEvent } from "react";
import { formatMoney } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import type { Projection } from "./compoundMath";
import { buildBalanceChart, H, W } from "./compoundGeometry";

/**
 * The balance, trading day by trading day, three ways: at the goal, at half
 * the goal, and the money that was put in.
 *
 * The band between the goal and the money put in is shaded, so the shading IS
 * the profit. Hovering reads off any day; with no hover the readout shows the
 * last day, which is the number the owner came for.
 */
const LEGEND = [
  { key: "goal", label: "At the goal", className: "bg-accent" },
  { key: "half", label: "At half the goal", className: "bg-series-3" },
  { key: "paid", label: "Paid in", className: "bg-ink-3" },
] as const;

export function CompoundBalanceSection({
  projection, half, monthlyDeposit, daysPerWeek, logScale, onLogScale,
}: {
  projection: Projection;
  half: Projection;
  monthlyDeposit: number;
  daysPerWeek: number;
  logScale: boolean;
  onLogScale: (on: boolean) => void;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const paid = useMemo(() => {
    const out: number[] = [];
    let total = projection.balances[0];
    let next = 0;
    for (let day = 0; day < projection.balances.length; day++) {
      while (next < projection.monthEnds.length && projection.monthEnds[next] <= day) {
        total += monthlyDeposit;
        next++;
      }
      out.push(total);
    }
    return out;
  }, [projection, monthlyDeposit]);

  const chart = useMemo(() => buildBalanceChart(
    { goal: projection.balances, half: half.balances, paid },
    projection.monthEnds, logScale,
  ), [projection, half, paid, logScale]);

  const day = hover ?? projection.tradingDays;
  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    setHover(chart.dayAt(((e.clientX - rect.left) / rect.width) * W));
  };
  const at = chart.points.goal[day];

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-3">
          {LEGEND.map((l) => (
            <span key={l.key} className="flex items-center gap-1.5 text-[11px] text-ink-2">
              <span className={cn("h-0.5 w-4 rounded", l.className)} />
              {l.label}
            </span>
          ))}
        </div>
        <button
          type="button"
          aria-pressed={logScale}
          onClick={() => onLogScale(!logScale)}
          title="A constant daily rate draws as a straight line on a log scale"
          className={cn(
            "rounded px-2 py-1 text-[11px] transition-colors",
            logScale ? "bg-surface-3 text-ink-1" : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
          )}
        >
          Log scale
        </button>
      </div>

      <div data-testid="cc-readout" className="num flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-3">
        <span>
          Day <span className="text-ink-1">{day}</span> · week{" "}
          <span className="text-ink-1">{Math.max(1, Math.ceil(day / daysPerWeek))}</span>
        </span>
        <span>Goal <span className="font-semibold text-accent">{formatMoney(projection.balances[day])}</span></span>
        <span>Half <span className="text-series-3">{formatMoney(half.balances[day])}</span></span>
        <span>Paid in <span className="text-ink-2">{formatMoney(paid[day])}</span></span>
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Projected balance by trading day"
        className="h-auto w-full cursor-crosshair rounded border border-line bg-surface-2"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id="cc-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-accent)" stopOpacity="0.32" />
            <stop offset="100%" stopColor="var(--color-accent)" stopOpacity="0.03" />
          </linearGradient>
        </defs>

        {chart.yTicks.map((t) => (
          <g key={t.value}>
            <line x1={chart.plot.left} y1={t.y} x2={chart.plot.right} y2={t.y}
              stroke="currentColor" strokeDasharray="3 3" className="text-line" />
            <text x={chart.plot.right + 6} y={t.y + 3} className="fill-ink-3 text-[10px]">
              {t.label}
            </text>
          </g>
        ))}
        {chart.xTicks.map((t, i) => (
          <text key={`${t.label}-${i}`} x={t.x} y={H - 8} textAnchor="middle"
            className="fill-ink-3 text-[10px]">
            {t.label}
          </text>
        ))}

        <path d={chart.goalArea} fill="url(#cc-fill)" stroke="none" />
        <path data-testid="cc-line-paid" d={chart.paths.paid} fill="none"
          stroke="var(--color-ink-3)" strokeWidth="1.25" strokeDasharray="2 3"
          vectorEffect="non-scaling-stroke" />
        <path data-testid="cc-line-half" d={chart.paths.half} fill="none"
          stroke="var(--color-series-3)" strokeWidth="1.5" strokeDasharray="6 4"
          vectorEffect="non-scaling-stroke" />
        <path data-testid="cc-line-goal" d={chart.paths.goal} fill="none"
          stroke="var(--color-accent)" strokeWidth="2" strokeLinejoin="round"
          vectorEffect="non-scaling-stroke" />

        {at && (
          <g>
            <line x1={at.x} y1={chart.plot.top} x2={at.x} y2={chart.plot.bottom}
              stroke="var(--color-ink-3)" strokeWidth="1" strokeOpacity="0.6" />
            <circle cx={chart.points.half[day].x} cy={chart.points.half[day].y} r="3"
              fill="var(--color-series-3)" />
            <circle cx={at.x} cy={at.y} r="4" fill="var(--color-accent)"
              stroke="var(--color-surface-2)" strokeWidth="2" />
          </g>
        )}
      </svg>
    </div>
  );
}
