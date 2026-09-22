import { useMemo } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, pnlColour } from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import type { ClosedTrades, CurvePoint } from "@/components/history/hooks/useClosedTrades";
import type { PollState } from "@/hooks/usePoll";
import { DashCard, Pill, Reading } from "./DashCard";
import { sparkPath } from "./dashboardMath";

const W = 320;
const H = 72;

/**
 * Realised profit across the window, as a shape.
 *
 * **Not account equity, and the card says so.** The line starts at zero and
 * counts closed trades; starting it at the account balance would mean
 * inventing where the account stood when the window opened, and a curve whose
 * zero is a guess reads as a loss on an account that never moved. The hero
 * card above answers "what is the account worth"; this answers "what did the
 * trading do".
 *
 * The full chart — money axis, dated ticks, filled area — is on the Analysis
 * tab. This is the same data through the same poll key, drawn small.
 */
export function EquityCard({ poll, days }: {
  poll: PollState<ClosedTrades>;
  days: number;
}) {
  const data = poll.data;
  const points = asArray<CurvePoint>(data?.curve?.points);
  const path = useMemo(
    () => sparkPath(points.map((p) => p.pnl), W, H), [points]);

  const net = data?.curve?.net ?? null;
  const up = (net ?? 0) >= 0;

  return (
    <DashCard
      title="Equity curve"
      icon="equity"
      badge={<Pill>last {days} days</Pill>}
      footnote="Realised profit from closed trades, starting at zero — not the account balance."
    >
      {!data ? (
        <EmptyState title={poll.error ? "Could not load the curve" : "Loading"}
          hint={poll.error?.message} />
      ) : data.error ? (
        <EmptyState title="No broker data" hint={data.error} />
      ) : !path ? (
        // Not a flat line at zero, which reads as "traded all month and broke
        // even".
        <EmptyState title="No closed trades in this window" />
      ) : (
        <>
          <svg
            viewBox={`0 0 ${W} ${H}`}
            role="img"
            aria-label="Realised profit and loss across the window"
            preserveAspectRatio="none"
            className={`h-20 w-full ${up ? "text-profit" : "text-loss"}`}
          >
            <path
              data-testid="dash-equity-path"
              d={path}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.75"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
          <dl className="mt-2 grid grid-cols-4 gap-x-3">
            <Reading label="Realised" value={formatMoney(net)} tone={pnlColour(net)} />
            <Reading label="Best" value={formatMoney(data.curve?.peak ?? null)} />
            <Reading label="Worst DD"
              value={formatMoney(data.curve?.max_drawdown ?? null)} tone="text-loss" />
            <Reading label="Trades" value={String(data.curve?.trades ?? 0)} />
          </dl>
        </>
      )}
    </DashCard>
  );
}
