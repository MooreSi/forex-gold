import { Calculator } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { formatMoney, formatPercent } from "@/components/shared/format";
import { LOT_STEPS } from "../hooks/useSetForgetController";
import { cn } from "@/lib/cn";

interface LotSizeSectionProps {
  lots: number | null;
  onPick: (lots: number) => void;
  suggested: number | null;
  riskPerLot: number | null;
  rewardPerLot: number | null;
  riskPct: number;
  balance: number | null;
}

/**
 * How much to trade, and what that is worth in money.
 *
 * Fixed steps rather than a free-text box. The number goes to a broker, and a
 * typed one is a typo with two extra zeros away from an order nobody meant —
 * "Size from risk %" is there for when a specific number IS wanted, and it
 * comes from the backend's own sizer rather than from anything typed here.
 *
 * The cash figures are a per-lot amount multiplied by the chosen lots. The
 * per-lot number comes from `fees_sizing.pnl`, this app's one P&L function;
 * re-deriving it in TypeScript would be a second answer to what the trade is
 * worth, differing in exactly the conditions nobody tests.
 */
export function LotSizeSection(props: LotSizeSectionProps) {
  const { lots, onPick, suggested, riskPerLot, rewardPerLot, riskPct, balance } = props;
  const risk = lots !== null && riskPerLot !== null ? riskPerLot * lots : null;
  const reward = lots !== null && rewardPerLot !== null ? rewardPerLot * lots : null;
  const ofBalance = risk !== null && balance ? (risk / balance) * 100 : null;

  return (
    <section className="rounded-lg border border-line bg-surface-1 p-4">
      <h3 className="text-xs font-semibold text-ink-1">Position size</h3>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {LOT_STEPS.map((step) => (
          <button
            key={step}
            type="button"
            onClick={() => onPick(step)}
            aria-pressed={lots === step}
            className={cn(
              "num rounded border px-2.5 py-1 text-[11px] transition-colors",
              lots === step
                ? "border-accent bg-accent/15 text-accent"
                : "border-line bg-surface-2 text-ink-2 hover:bg-surface-3",
            )}
          >
            {step.toFixed(2)}
          </button>
        ))}
        <Button
          variant="ghost"
          onClick={() => suggested && onPick(suggested)}
          disabledReason={
            suggested
              ? null
              : balance
                ? "There is no setup to size against yet."
                : "The account balance could not be read, so a risk-based size "
                  + "cannot be computed."
          }
          title={`Size it from ${formatPercent(riskPct)} of the balance`}
        >
          <Calculator size={12} />
          Size from risk
          {suggested && <span className="num text-ink-3">{suggested.toFixed(2)}</span>}
        </Button>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Figure label="Lots" value={lots !== null ? lots.toFixed(2) : "—"} />
        <Figure
          label="Risked"
          value={risk !== null ? formatMoney(risk) : "—"}
          tone="loss"
          hint={ofBalance !== null ? `${formatPercent(ofBalance)} of balance` : undefined}
        />
        <Figure
          label="Target pays"
          value={reward !== null ? formatMoney(reward) : "—"}
          tone="profit"
        />
        <Figure
          label="Balance"
          value={balance ? formatMoney(balance) : "unavailable"}
        />
      </dl>

      {lots === null && (
        <p className="mt-2 text-[10px] text-ink-3">
          No size chosen yet. Pick one above, or the order is sized from your
          Risk per trade % and the stop distance when it is placed.
        </p>
      )}
    </section>
  );
}

function Figure({ label, value, tone, hint }: {
  label: string; value: string; tone?: "profit" | "loss"; hint?: string;
}) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-ink-3">{label}</dt>
      <dd className={cn("num text-sm font-semibold",
                        tone === "profit" ? "text-profit"
                        : tone === "loss" ? "text-loss" : "text-ink-1")}>
        {value}
      </dd>
      {hint && <p className="text-[10px] text-ink-3">{hint}</p>}
    </div>
  );
}
