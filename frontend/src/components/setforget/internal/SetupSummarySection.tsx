import { ArrowDownRight, ArrowUpRight, Clock, Zap } from "lucide-react";
import type { SetForgetCandidate, SetForgetEvidence } from "@/api/types";
import { formatMoney, formatPrice } from "@/components/shared/format";
import { cn } from "@/lib/cn";

interface SetupSummarySectionProps {
  candidate: SetForgetCandidate;
  evidence: SetForgetEvidence;
  minRr: number;
  riskMoney: number | null;
  rewardMoney: number | null;
  invalidations: string[];
}

const BIAS_TONE: Record<string, string> = {
  bullish: "text-profit",
  bearish: "text-loss",
  ranging: "text-warning",
  unknown: "text-ink-3",
};

/**
 * The proposed trade, in the order a person checks it: which way, at what
 * price, where it is wrong, where it pays, and how those two compare.
 *
 * `invalidations` is rendered loudly and never collapsed. A setup that breaks
 * the method's own rules looks exactly as tidy as one that does not — same
 * card, same numbers, same confidence — and the refusal is the only thing on
 * screen that distinguishes them.
 */
export function SetupSummarySection(props: SetupSummarySectionProps) {
  const { candidate, evidence, minRr, riskMoney, rewardMoney, invalidations } = props;
  const long = candidate.direction === "BUY";
  const Arrow = long ? ArrowUpRight : ArrowDownRight;
  const resting = candidate.order_type === "limit";
  const thin = candidate.rr !== null && candidate.rr < minRr;

  return (
    <section className="rounded-lg border border-line bg-surface-1 p-4">
      <header className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-semibold",
            long ? "bg-profit/15 text-profit" : "bg-loss/15 text-loss",
          )}
        >
          <Arrow size={13} />
          {candidate.direction} XAUUSD
        </span>
        <span className="inline-flex items-center gap-1 rounded-full border border-line
                         bg-surface-2 px-2.5 py-1 text-[11px] text-ink-2">
          {resting ? <Clock size={12} /> : <Zap size={12} />}
          {resting ? "Resting limit order" : "Market order"}
        </span>
        {candidate.rr !== null && (
          <span
            className={cn(
              "num inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-semibold",
              thin ? "bg-loss/15 text-loss" : "bg-surface-3 text-ink-1",
            )}
          >
            1:{candidate.rr.toFixed(2)} reward-to-risk
          </span>
        )}
      </header>

      <p className="mt-2 text-[11px] leading-relaxed text-ink-3">
        {resting
          ? `The order waits at ${formatPrice(candidate.entry)} for price to come `
            + "back to the zone. Nothing happens until it does — that is the "
            + "set-and-forget part."
          : "Price is already at the zone, so this fills now at the market."}
      </p>

      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <Level label="Entry" price={candidate.entry} />
        <Level
          label="Stop loss"
          price={candidate.stop_loss}
          tone="loss"
          points={candidate.risk}
          money={riskMoney}
        />
        <Level
          label="Take profit"
          price={candidate.take_profit}
          tone="profit"
          points={candidate.reward}
          money={rewardMoney}
        />
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
        <Fact label="Weekly" value={evidence.weekly_bias} tone />
        <Fact label="Daily" value={evidence.daily_bias} tone />
        <Fact label={evidence.entry_timeframe} value={evidence.entry_bias} tone />
        <Fact
          label="Zone"
          value={candidate.zone
            ? `${formatPrice(candidate.zone.low)}–${formatPrice(candidate.zone.high)}`
            : "—"}
        />
      </dl>

      {invalidations.length > 0 && (
        <div
          role="alert"
          className="mt-3 rounded-md border border-loss/40 bg-loss/10 px-3 py-2"
        >
          <p className="text-[11px] font-semibold text-loss">
            This setup does not meet the method&apos;s rules
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11px] text-loss/90">
            {invalidations.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </div>
      )}
    </section>
  );
}

interface LevelProps {
  label: string;
  price: number;
  tone?: "profit" | "loss";
  points?: number;
  money?: number | null;
}

function Level({ label, price, tone, points, money }: LevelProps) {
  return (
    <div className="rounded-md border border-line bg-surface-2/50 px-3 py-2">
      <p className="text-[10px] uppercase tracking-wider text-ink-3">{label}</p>
      <p className={cn("num text-sm font-semibold",
                       tone === "profit" ? "text-profit"
                       : tone === "loss" ? "text-loss" : "text-ink-1")}>
        {formatPrice(price)}
      </p>
      {points !== undefined && (
        <p className="num mt-0.5 text-[10px] text-ink-3">
          {points.toFixed(2)} pts
          {/* Omitted, not zeroed, until a lot size has been chosen: "$0.00"
              is a real answer and it is not this one. */}
          {money != null && <> · {formatMoney(money)}</>}
        </p>
      )}
    </div>
  );
}

function Fact({ label, value, tone }: { label: string; value: string; tone?: boolean }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-ink-3">{label}</dt>
      <dd className={cn("text-[11px] font-medium capitalize",
                        tone ? BIAS_TONE[value] ?? "text-ink-2" : "text-ink-1")}>
        {value}
      </dd>
    </div>
  );
}
