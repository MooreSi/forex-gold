import { useMemo } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, pnlColour } from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { useClosedTrades, type CurvePoint } from "../hooks/useClosedTrades";

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
const W = 600;
const H = 160;
const PAD = 4;

function path(points: CurvePoint[]): { line: string; zero: number } {
  const values = points.map((p) => p.pnl);
  const lo = Math.min(0, ...values);
  const hi = Math.max(0, ...values);
  // A window that never moved would divide by zero; one point of range keeps
  // the line flat and on screen instead.
  const span = hi - lo || 1;

  const y = (v: number) => PAD + (hi - v) / span * (H - PAD * 2);
  const x = (i: number) => (points.length === 1
    ? W / 2
    : (i / (points.length - 1)) * W);

  return {
    line: points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.pnl).toFixed(1)}`).join(" "),
    zero: y(0),
  };
}

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
  const geometry = useMemo(() => (points.length ? path(points) : null), [points]);

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
        preserveAspectRatio="none"
        role="img"
        aria-label="Realised profit and loss across the window"
        className="h-40 w-full rounded border border-line bg-surface-2"
      >
        {/* The zero line is the whole point of a P&L curve: above it the
            window made money, below it the window lost money. */}
        <line x1="0" y1={geometry.zero} x2={W} y2={geometry.zero}
          stroke="currentColor" strokeWidth="1" strokeDasharray="3 3"
          className="text-line" />
        <path
          data-testid="equity-path"
          d={geometry.line}
          fill="none"
          strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
          stroke="currentColor"
          className={up ? "text-profit" : "text-loss"}
        />
      </svg>

      <p className="text-[11px] text-ink-3">
        <strong>Realised</strong> profit and loss from closed trades in this
        window, starting at zero — not the account balance, which is in the
        header. Open positions are not in it.
      </p>
    </div>
  );
}
