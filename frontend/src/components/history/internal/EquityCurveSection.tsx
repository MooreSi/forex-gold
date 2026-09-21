import { useMemo } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, pnlColour } from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { formatBrokerTime } from "@/components/shared/format";
import { useClosedTrades, type CurvePoint } from "../hooks/useClosedTrades";
import { buildGeometry, H, M, W } from "./equityGeometry";

/**
 * Realised P&L across the window, trade by trade.
 *
 * **Not account equity, and the panel says so.** Starting the line at the
 * account balance would mean inventing where the account stood when the
 * window opened — that figure is nowhere in the deal history, and a curve
 * whose zero is a guess reads as a loss when the account never moved. The
 * header's whole-life P&L answers "where is the account overall"; this
 * answers "what did the trading do over these N days".
 *
 * Drawn as a plain SVG path rather than a charting library: it is one series
 * of a few hundred points with no interaction, and the chart library already
 * in this app is the candlestick one, which is a different job entirely.
 *
 * The drawdown figure is measured from the running peak, not from zero. Up 80
 * and back to 30 is a 50 drawdown, not a 30 profit with nothing wrong.
 */
function Stat({ label, value, testId, className }: {
  label: string; value: string; testId: string; className?: string;
}) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-ink-3">{label}</p>
      <p data-testid={testId} className={cn("num text-sm font-semibold text-ink-1", className)}>
        {value}
      </p>
    </div>
  );
}

export function EquityCurveSection({ days }: { days: number }) {
  const poll = useClosedTrades(days);
  const data = poll.data;

  const curve = data?.curve;
  const points = asArray<CurvePoint>(curve?.points);
  const geometry = useMemo(
    () => buildGeometry(points, formatBrokerTime), [points]);

  if (!data) {
    return <EmptyState title={poll.error ? "Could not load the curve" : "Loading"}
      hint={poll.error?.message} />;
  }
  if (data.error) {
    return <EmptyState title="No broker data" hint={data.error} />;
  }
  if (!geometry) {
    // Not a flat line at zero, which reads as "traded all month and broke
    // even".
    return <EmptyState title="No closed trades in this window" />;
  }

  const net = curve?.net ?? 0;
  const up = net >= 0;
  // The gradient is referenced by id, so two of these on one page would share
  // one definition and the second would take the first's colour.
  const gradientId = `equity-fill-${days}`;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Realised" value={formatMoney(net)} testId="curve-net"
          className={pnlColour(net)} />
        <Stat label="Best it got" value={formatMoney(curve?.peak ?? 0)} testId="curve-peak" />
        <Stat label="Worst drawdown" value={formatMoney(curve?.max_drawdown ?? 0)}
          testId="curve-drawdown" className="text-loss" />
        <Stat label="Trades" value={String(curve?.trades ?? 0)} testId="curve-trades" />
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Realised profit and loss across the window"
        className="h-56 w-full rounded border border-line bg-surface-2"
      >
        <defs>
          {/* The gradient the NiceGUI chart had: the line's own colour at the
              top, fading to nothing at the zero line. It is what makes the
              shape readable at a glance rather than a wire on a box. */}
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0.02" />
          </linearGradient>
        </defs>

        {/* Gridlines and the money axis. Without them the height of the line
            says nothing: +$120 and +$12,000 draw the identical picture. */}
        {geometry.yTicks.map((t) => (
          <g key={t.value}>
            <line
              x1={M.left} y1={t.y} x2={W - M.right} y2={t.y}
              stroke="currentColor" strokeWidth="1"
              strokeDasharray={t.value === 0 ? undefined : "3 3"}
              className={t.value === 0 ? "text-ink-3/50" : "text-line"}
            />
            <text
              data-testid={`curve-y-${t.value}`}
              x={W - M.right + 6} y={t.y + 3}
              className="fill-ink-3 text-[10px]"
            >
              {t.label}
            </text>
          </g>
        ))}

        {/* When each end of the window was. Three labels, not one per point:
            a few hundred dates is a smear, and the ends plus the middle are
            what answers "when was this". */}
        {geometry.xTicks.map((t, i) => (
          <text
            key={`${t.label}-${i}`}
            data-testid={`curve-x-${i}`}
            x={t.x} y={H - 8}
            textAnchor={i === 0 ? "start" : i === geometry.xTicks.length - 1 ? "end" : "middle"}
            className="fill-ink-3 text-[10px]"
          >
            {t.label}
          </text>
        ))}

        <g className={up ? "text-profit" : "text-loss"}>
          <path data-testid="equity-area" d={geometry.area}
            fill={`url(#${gradientId})`} stroke="none" />
          <path
            data-testid="equity-path"
            d={geometry.line}
            fill="none"
            strokeWidth="1.75"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
            stroke="currentColor"
            className={up ? "text-profit" : "text-loss"}
          />
          {geometry.last && (
            <circle cx={geometry.last.x} cy={geometry.last.y} r="2.5"
              fill="currentColor" />
          )}
        </g>
      </svg>

      <p className="text-[11px] text-ink-3">
        <span data-testid="curve-last" className={cn("num font-semibold", pnlColour(net))}>
          {formatMoney(net)}
        </span>{" "}
        where the window ended.{" "}
        <strong>Realised</strong> profit and loss from closed trades in this
        window, starting at zero — not the account balance, which is in the
        header. Open positions are not in it.
      </p>
    </div>
  );
}
