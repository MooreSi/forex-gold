import { CircleSlash } from "lucide-react";
import type { SetForgetEvidence } from "@/api/types";
import { cn } from "@/lib/cn";

const BIAS_TONE: Record<string, string> = {
  bullish: "text-profit",
  bearish: "text-loss",
  ranging: "text-warning",
  unknown: "text-ink-3",
};

/**
 * No trade, and which rule said so.
 *
 * "No setup" on a page someone has just opened is indistinguishable from a
 * broken one, and the temptation then is to loosen a rule until something
 * appears. Naming the rule is what makes waiting feel like the method working
 * rather than the feature failing — most of the time there IS no trade, and
 * that is the point of the method rather than a shortcoming of it.
 */
export function NoSetupSection({ reason, evidence }: {
  reason: string; evidence: SetForgetEvidence;
}) {
  return (
    <section className="rounded-lg border border-line bg-surface-1 px-4 py-5">
      <p className="flex items-center gap-2 text-sm font-semibold text-ink-1">
        <CircleSlash size={15} className="text-ink-3" />
        No setup right now
      </p>
      <p className="mt-1.5 max-w-2xl text-[11px] leading-relaxed text-ink-2">
        {reason || "The rules did not produce a candidate from this chart."}
      </p>

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2">
        <Read label="Weekly" value={evidence.weekly_bias} />
        <Read label="Daily" value={evidence.daily_bias} />
        <Read label={evidence.entry_timeframe} value={evidence.entry_bias} />
        <Read label="Zones found" value={String(evidence.zones.length)} plain />
      </dl>

      <p className="mt-3 text-[10px] leading-relaxed text-ink-3">
        Set &amp; Forget spends most of its time waiting. The chart above is
        still live — the zones are drawn, and the section will propose a trade
        as soon as one meets the rules.
      </p>
    </section>
  );
}

function Read({ label, value, plain }: { label: string; value: string; plain?: boolean }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-ink-3">{label}</dt>
      <dd className={cn("text-xs font-medium capitalize",
                        plain ? "num text-ink-1" : BIAS_TONE[value] ?? "text-ink-2")}>
        {value}
      </dd>
    </div>
  );
}
