import { EmptyState } from "@/components/shared/EmptyState";
import { StatCard } from "@/components/shared/StatCard";
import {
  formatMoney, formatPercent, formatSignedMoney, pnlColour,
} from "@/components/shared/format";

function num(source: Record<string, unknown>, key: string): number | null {
  const raw = source[key];
  return typeof raw === "number" ? raw : null;
}

/**
 * The account's headline numbers for the selected window.
 *
 * An empty payload means the bridge could not answer. It renders as "no broker
 * data", never as zeros — a zeroed row reads as a flat month that happened.
 */
export function PerformanceSection({ performance }: { performance: Record<string, unknown> }) {
  if (Object.keys(performance).length === 0) {
    return (
      <EmptyState
        title="No broker data for this window"
        hint="These numbers come from MT5 through the bridge. Check the bridge indicator in the header."
      />
    );
  }

  const pnl = num(performance, "total_net_pnl");
  return (
    <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
      <StatCard label="Net P&L" value={formatSignedMoney(pnl)} valueClassName={pnlColour(pnl)} />
      <StatCard label="Closed" value={num(performance, "closed_trades") ?? "—"} />
      <StatCard label="Win rate" value={formatPercent(num(performance, "win_rate_pct"))} />
      <StatCard
        label="Profit factor"
        value={(num(performance, "profit_factor") ?? 0).toFixed(2)}
        hint="Gross profit divided by gross loss. Above 1.0 is profitable overall."
      />
      <StatCard
        label="Max drawdown"
        value={formatPercent(num(performance, "max_drawdown_pct"))}
        valueClassName="text-loss"
        hint="The worst fall from a peak over this window."
      />
      <StatCard label="Balance" value={formatMoney(num(performance, "balance"))} />
    </div>
  );
}
