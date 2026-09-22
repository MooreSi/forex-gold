import { EmptyState } from "@/components/shared/EmptyState";
import {
  formatPercent, formatSignedMoney, pnlColour,
} from "@/components/shared/format";
import { DashCard, Pill, Reading } from "./DashCard";

function num(source: Record<string, unknown>, key: string): number | null {
  const raw = source[key];
  return typeof raw === "number" ? raw : null;
}

/**
 * What the account has actually done over the window.
 *
 * The same six figures the Analysis tab leads with, from the same payload —
 * this card and that tab share the poll key, so they cannot disagree about a
 * month. Every one of them is computed by the backend from the broker's deal
 * history.
 *
 * An empty payload means the bridge could not answer, and it renders as
 * "no broker data" rather than as zeros. A zeroed row reads as a flat month
 * that really happened.
 */
export function PerformanceCard({ performance, days }: {
  performance: Record<string, unknown>;
  days: number;
}) {
  const pnl = num(performance, "total_net_pnl");
  const factor = num(performance, "profit_factor");

  return (
    <DashCard
      title="Trading performance"
      icon="performance"
      badge={<Pill>last {days} days</Pill>}
      footnote="Closed trades only, from the broker's deal history. Open positions are in the Positions card."
    >
      {Object.keys(performance).length === 0 ? (
        <EmptyState
          title="No broker data for this window"
          hint="These come from MT5 through the bridge."
        />
      ) : (
        <>
          <p className={`num text-2xl font-bold ${pnlColour(pnl)}`}>
            {formatSignedMoney(pnl)}
          </p>
          <p className="mb-2 text-[10px] text-ink-3">net profit and loss</p>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-4">
            <Reading label="Closed" value={num(performance, "closed_trades") ?? "—"} />
            <Reading label="Win rate"
              value={formatPercent(num(performance, "win_rate_pct"))} />
            <Reading
              label="Profit factor"
              value={factor == null ? "—" : factor.toFixed(2)}
              hint="Gross profit divided by gross loss. Above 1.0 is profitable overall."
              tone={factor == null ? undefined
                : factor >= 1 ? "text-profit" : "text-loss"}
            />
            <Reading
              label="Max drawdown"
              value={formatPercent(num(performance, "max_drawdown_pct"))}
              tone="text-loss"
              hint="The worst fall from a peak over this window."
            />
          </dl>
        </>
      )}
    </DashCard>
  );
}
