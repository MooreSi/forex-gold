import { useMemo, useState } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatBrokerTime, formatMoney, formatSignedMoney, pnlColour } from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { useClosedTrades, type CurvePoint } from "../hooks/useClosedTrades";
import { useElementWidth } from "../hooks/useElementWidth";
import { buildGeometry, W } from "./equityGeometry";
import { sliceCurve } from "./equityView";
import { EquityCurveView } from "./EquityCurveView";
import { EquityRangeSection } from "./EquityRangeSection";

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
 * Drawn as SVG rather than with a charting library: one series, and the
 * chart library in this app is the candlestick one. It is drawn at the
 * frame's MEASURED width (owner, 2026-10-09: "needs to be wider"). A fixed
 * viewBox scaled into a fixed height can never be wider than its aspect
 * ratio allows, which left it ~730px wide in the middle of a wide panel.
 *
 * The slider narrows the view inside the loaded window, and every figure
 * above the chart is measured over the view, rebased to start at zero (see
 * `equityView.ts`). With the whole window in view they are the backend's.
 *
 * The drawdown figure is measured from the running peak, not from zero. Up 80
 * and back to 30 is a 50 drawdown, not a 30 profit with nothing wrong.
 */
const CHART_H = 300;

function Stat({ label, value, testId, className, sub }: {
  label: string; value: string; testId: string; className?: string; sub?: string;
}) {
  return (
    <div className="rounded-md border border-line bg-surface-2/60 px-3 py-2">
      <p className="text-[10px] uppercase tracking-wider text-ink-3">{label}</p>
      <p data-testid={testId} className={cn("num text-base font-semibold text-ink-1", className)}>
        {value}
      </p>
      {sub && <p className="num text-[10px] text-ink-3">{sub}</p>}
    </div>
  );
}

export function EquityCurveSection({ days }: { days: number }) {
  const poll = useClosedTrades(days);
  const data = poll.data;
  const [frame, width] = useElementWidth<HTMLDivElement>();
  // Null is "the whole window", so a trade that closes while the tab is open
  // extends the view instead of falling just outside it.
  const [range, setRange] = useState<[number, number] | null>(null);

  const points = asArray<CurvePoint>(data?.curve?.points);
  const min = points.length ? points[0].ts : 0;
  const max = points.length ? points[points.length - 1].ts : 0;
  const from = range ? Math.max(min, Math.min(range[0], max)) : min;
  const to = range ? Math.min(max, Math.max(range[1], from)) : max;

  const view = useMemo(() => sliceCurve(points, from, to), [points, from, to]);
  const geometry = useMemo(
    () => (view ? buildGeometry(view.points, formatBrokerTime,
      { w: width ?? W, h: CHART_H }) : null),
    [view, width]);

  if (!data) {
    return <EmptyState title={poll.error ? "Could not load the curve" : "Loading"}
      hint={poll.error?.message} />;
  }
  if (data.error) {
    return <EmptyState title="No broker data" hint={data.error} />;
  }
  if (!points.length) {
    // Not a flat line at zero, which reads as "traded all month and broke
    // even".
    return <EmptyState title="No closed trades in this window" />;
  }

  const whole = from <= min && to >= max;
  // The whole window reports the backend's own figures, not a recount of
  // its points: the backend is the one source both this and the table use.
  const curve = data.curve;
  const stats = whole
    ? { net: curve?.net ?? 0, peak: curve?.peak ?? 0,
        maxDrawdown: curve?.max_drawdown ?? 0, trades: curve?.trades ?? 0 }
    : { net: view?.net ?? 0, peak: view?.peak ?? 0,
        maxDrawdown: view?.maxDrawdown ?? 0, trades: view?.trades ?? 0 };
  const net = stats.net;
  const up = net >= 0;
  const wins = view ? view.points.slice(1).filter((p, i) => p.pnl > view.points[i].pnl).length : 0;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label={whole ? "Realised" : "Realised in view"} value={formatMoney(net)}
          testId="curve-net" className={pnlColour(net)} />
        <Stat label="Best it got" value={formatMoney(stats.peak)} testId="curve-peak" />
        <Stat label="Worst drawdown" value={formatMoney(stats.maxDrawdown)}
          testId="curve-drawdown" className="text-loss" />
        <Stat label="Trades" value={String(stats.trades)} testId="curve-trades"
          sub={view?.trades ? `${Math.round((wins / view.trades) * 100)}% closed in profit` : undefined} />
      </div>

      <div className="rounded-lg border border-line bg-gradient-to-b from-surface-2/70
                      to-surface-1 p-3">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <p className="text-xs font-semibold text-ink-1">
            Realised P&amp;L{" "}
            <span className="font-normal text-ink-3">
              {whole ? `last ${days} days` : "in the selected window"}
            </span>
          </p>
          <span data-testid="curve-last"
            className={cn("num text-sm font-semibold", pnlColour(net))}>
            {formatSignedMoney(net).replace(/^\+/, "")}
          </span>
        </div>

        <div ref={frame} className="w-full">
          {geometry && view ? (
            <EquityCurveView geometry={geometry} points={view.points} up={up} />
          ) : (
            <div className="flex h-[300px] items-center justify-center text-xs text-ink-3">
              No trades closed between {formatBrokerTime(from)} and {formatBrokerTime(to)}.
            </div>
          )}
        </div>

        <div className="mt-3 border-t border-line/60 pt-3">
          <EquityRangeSection points={points} min={min} max={max} from={from} to={to}
            onChange={(a, b) => setRange(a <= min && b >= max ? null : [a, b])} />
        </div>
      </div>

      <p className="text-[11px] text-ink-3">
        <strong>Realised</strong> profit and loss from closed trades in this
        window, starting at zero — not the account balance, which is in the
        header. Open positions are not in it. Drag the handles under the chart
        to look at part of the window; the figures above follow it.
      </p>
    </div>
  );
}
