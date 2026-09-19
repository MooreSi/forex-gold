import { ArrowDownRight, ArrowUpRight, WifiOff } from "lucide-react";
import type { Tick } from "@/api/types";
import { cn } from "@/lib/cn";

/**
 * The six figures along the top bar: price, cash, and whether you are up.
 *
 * Restored from the NiceGUI header, which built them in a 1-second update
 * loop. The grouping is the original's and it carries meaning: BID/ASK/spread
 * is the market, MT5 BAL/free is what the account holds, EQUITY/P&L is what it
 * is worth right now against what was paid in. An operator glances at these
 * rather than reading them, so the rules are about what a glance can get
 * wrong.
 *
 * **A missing number is an em dash, never a zero.** `$0.00` for an account the
 * bridge could not read looks like a blown account.
 *
 * **A loss always carries its sign.** The whole-life P&L is the one figure
 * that is routinely large and negative.
 *
 * The whole-life figure is absent, not zero, when the deposit history cannot
 * be read — otherwise the entire equity shows as profit, which is the most
 * flattering possible wrong answer.
 */
interface HeaderStatsProps {
  tick: Tick | null;
  account: Record<string, unknown> | null;
  /** Equity minus net deposits. Null when the deposits are not known. */
  lifetimePnl: number | null;
  stale: boolean;
}

const DASH = "—";

function money(value: unknown, dp = 2): string {
  const n = typeof value === "number" ? value : Number(value);
  if (value == null || !Number.isFinite(n)) return DASH;
  return `$${n.toLocaleString("en-GB", {
    minimumFractionDigits: dp, maximumFractionDigits: dp,
  })}`;
}

function Stat({ label, children, testId }: {
  label: string; children: React.ReactNode; testId: string;
}) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-[10px] font-medium uppercase tracking-wider text-ink-3">
        {label}
      </span>
      <span data-testid={testId} className="num text-xs font-semibold text-ink-1">
        {children}
      </span>
    </div>
  );
}

function Divider({ className }: { className?: string } = {}) {
  return <span aria-hidden className={cn("h-6 w-px shrink-0 bg-line", className)} />;
}

export function HeaderStats({ tick, account, lifetimePnl, stale }: HeaderStatsProps) {
  const spread = tick?.spread_points;
  const up = lifetimePnl != null && lifetimePnl >= 0;

  return (
    // `min-w-0` and the responsive hiding below are not decoration. Six
    // figures plus the brand, the badge and the right-hand controls forced the
    // document to 1153px at a 1024px viewport on 2026-09-19, which scrolled
    // the WHOLE APP sideways and clipped the panel beneath it. The least
    // important figures drop out first; bid and ask never do.
    <div className="flex min-w-0 items-center gap-2 lg:gap-3">
      <Divider />

      <Stat label="BID" testId="stat-bid">
        <span className="text-profit">{money(tick?.bid)}</span>
      </Stat>
      <Stat label="ASK" testId="stat-ask">
        <span className="text-loss">{money(tick?.ask)}</span>
      </Stat>
      <span data-testid="stat-spread" className="num hidden text-[10px] text-ink-3 lg:inline">
        {spread == null || !Number.isFinite(spread)
          ? `spr:${DASH}`
          : `spr:${Math.round(spread)}pt`}
      </span>
      {stale && (
        <span
          data-testid="stat-stale"
          className="flex items-center gap-1 rounded bg-warning/15 px-1.5 py-0.5 text-[10px] text-warning"
          title="This price has stopped updating."
        >
          <WifiOff size={10} /> stale
        </span>
      )}

      <Divider className="hidden xl:block" />

      <div className="hidden items-center gap-2 xl:flex">
        <Stat label="MT5 BAL" testId="stat-balance">{money(account?.["balance"])}</Stat>
        <span data-testid="stat-free" className="num text-[10px] text-ink-3">
        {/* Headroom, not an accounting figure. Pennies on it are noise next to
            the balance it sits under. */}
          free:{money(account?.["margin_free"], 0)}
        </span>
      </div>

      <Divider />

      <Stat label="EQUITY" testId="stat-equity">{money(account?.["equity"])}</Stat>
      {lifetimePnl != null && (
        <span
          data-testid="stat-lifetime"
          className={cn(
            "num flex items-center gap-0.5 text-[10px] font-semibold",
            up ? "text-profit" : "text-loss",
          )}
          title="Equity minus everything paid in: the account's whole life, including open trades, swap and commission."
        >
          {up ? <ArrowUpRight size={10} /> : <ArrowDownRight size={10} />}
          P&amp;L:${lifetimePnl >= 0 ? "+" : "-"}
          {Math.abs(lifetimePnl).toFixed(2)}
        </span>
      )}
    </div>
  );
}
