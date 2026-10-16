import { EmptyState } from "@/components/shared/EmptyState";
import {
  formatPercent, formatSignedMoney, pnlColour,
} from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { DASHBOARD_WINDOWS, type DashboardWindow } from "../hooks/useDashboardWindow";
import { DashCard, Reading } from "./DashCard";

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
 *
 * The window buttons live here and move the Equity curve card too (owner,
 * 2026-10-09). They choose what is SHOWN and nothing else.
 */
export function PerformanceCard({ performance, range, onWindow }: {
  performance: Record<string, unknown>;
  range: DashboardWindow;
  onWindow: (days: number) => void;
}) {
  const pnl = num(performance, "total_net_pnl");
  const factor = num(performance, "profit_factor");
  // Winners and no losers: no finite factor, and the backend's 0 for it would
  // print as a red 0.00 (owner, 2026-10-16).
  const noLosses = performance.no_losses === true;

  return (
    <DashCard
      title="Trading performance"
      icon="performance"
      actions={
        <div className="flex items-center gap-0.5 rounded bg-surface-2 p-0.5">
          {DASHBOARD_WINDOWS.map((w) => (
            <button
              key={w.days}
              onClick={() => onWindow(w.days)}
              aria-pressed={w.days === range.days}
              aria-label={w.name}
              title={w.name}
              className={cn(
                "num rounded px-1.5 py-0.5 text-[10px] transition-colors",
                w.days === range.days
                  ? "bg-accent/20 text-accent"
                  : "text-ink-3 hover:bg-surface-3 hover:text-ink-2",
              )}
            >
              {w.short}
            </button>
          ))}
        </div>
      }
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
          <dl className="grid grid-cols-2 gap-x-3 gap-y-2 @md:grid-cols-4">
            <Reading label="Closed" value={num(performance, "closed_trades") ?? "—"} />
            <Reading label="Win rate"
              value={formatPercent(num(performance, "win_rate_pct"))} />
            <Reading
              label="Profit factor"
              value={noLosses ? "No losses" : factor == null ? "—" : factor.toFixed(2)}
              hint="Gross profit divided by gross loss. Above 1.0 is profitable overall. No losses means every closed trade in this window won."
              tone={noLosses ? "text-profit" : factor == null ? undefined
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
