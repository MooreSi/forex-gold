import type { SetForgetEvidence } from "@/api/types";
import { formatPrice } from "@/components/shared/format";
import { cn } from "@/lib/cn";

/**
 * The instruments the method reads, as numbers rather than as checklist items.
 *
 * The checklist already says whether each one agrees, in a sentence. This says
 * what each one actually IS, which is what a person checks when they disagree
 * with the sentence — and disagreeing with it is a legitimate thing to do,
 * since these are the community's confluence list rather than a published rule.
 *
 * Every value is measured by the backend. Nothing here computes an average or
 * a ratio; the EMA comes from the same `ema_series` the engine's signal
 * snapshot uses, and re-deriving any of it in TypeScript would be a second
 * answer to a question the chart beside it has already answered.
 *
 * A value that could not be read shows as an em dash, never as zero. An EMA of
 * 0.00 on gold is not a number anyone should be shown as if it were real.
 */
export function IndicatorStrip({ evidence }: { evidence: SetForgetEvidence }) {
  const { ema_fast: fast, ema_slow: slow, rsi, atr, fib } = evidence;
  const trendUp = fast !== null && slow !== null ? fast > slow : null;

  return (
    <section className="rounded-lg border border-line bg-surface-1 px-4 py-3">
      <h3 className="text-[10px] uppercase tracking-wider text-ink-3">
        Instruments on the {evidence.entry_timeframe} chart
      </h3>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-5">
        <Reading
          label="EMA 50"
          value={formatPrice(fast)}
          swatch="bg-accent"
          hint={trendUp === null ? undefined : trendUp ? "above 200" : "below 200"}
        />
        <Reading label="EMA 200" value={formatPrice(slow)} swatch="bg-remote" />
        <Reading
          label="RSI 14"
          value={rsi === null ? "—" : rsi.toFixed(1)}
          tone={rsi === null ? undefined
            : rsi >= 70 ? "loss" : rsi <= 30 ? "profit" : undefined}
          hint={rsi === null ? undefined
            : rsi >= 70 ? "overbought" : rsi <= 30 ? "oversold" : "mid-range"}
        />
        <Reading
          label="ATR 14"
          value={atr ? atr.toFixed(2) : "—"}
          hint={atr ? "sets zone width" : undefined}
        />
        <Reading
          label="Pullback"
          value={fib === null ? "—" : `${(fib * 100).toFixed(1)}%`}
          hint={fib === null ? "no completed leg" : "of the last leg"}
        />
      </dl>
    </section>
  );
}

interface ReadingProps {
  label: string;
  value: string;
  hint?: string;
  tone?: "profit" | "loss";
  /** A dot in the colour this line is drawn on the chart, so the two match. */
  swatch?: string;
}

function Reading({ label, value, hint, tone, swatch }: ReadingProps) {
  return (
    <div>
      <dt className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider
                     text-ink-3">
        {swatch && <span className={cn("h-1.5 w-1.5 rounded-full", swatch)} />}
        {label}
      </dt>
      <dd className={cn("num text-sm font-semibold",
                        tone === "profit" ? "text-profit"
                        : tone === "loss" ? "text-loss" : "text-ink-1")}>
        {value}
      </dd>
      {hint && <p className="text-[10px] text-ink-3">{hint}</p>}
    </div>
  );
}
