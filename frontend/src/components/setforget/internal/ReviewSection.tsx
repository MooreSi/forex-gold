import { Bot, CircleAlert, CircleCheck, CircleSlash, TriangleAlert } from "lucide-react";
import type { SetForgetReview } from "@/api/types";
import { cn } from "@/lib/cn";

const VERDICT = {
  take: { icon: CircleCheck, label: "Take it", tone: "text-profit",
          ring: "border-profit/35 bg-profit/[0.07]" },
  adjust: { icon: TriangleAlert, label: "Take it, adjusted", tone: "text-warning",
            ring: "border-warning/35 bg-warning/[0.07]" },
  skip: { icon: CircleSlash, label: "Skip it", tone: "text-loss",
          ring: "border-loss/35 bg-loss/[0.07]" },
} as const;

/**
 * What the configured model made of the setup.
 *
 * A "skip" is shown as prominently as a "take", and the setup stays on screen
 * underneath it. Hiding a rejected setup would leave the operator unable to
 * judge whether they agree with the objection — and disagreeing with it is a
 * legitimate thing to do, since the model is reviewing, not deciding.
 *
 * `levels_rejected` is the important one. It means the model proposed levels
 * that broke the method's own rules, they were discarded, and what is on
 * screen is the rules' own numbers. That has to be visible: silently keeping
 * the deterministic levels would let the page claim a review it did not get.
 */
export function ReviewSection({ review }: { review: SetForgetReview }) {
  if (review.error) {
    return (
      <section
        role="status"
        className="rounded-lg border border-warning/35 bg-warning/[0.07] px-4 py-3"
      >
        <p className="flex items-center gap-2 text-xs font-semibold text-warning">
          <CircleAlert size={13} /> The AI review did not complete
        </p>
        <p className="mt-1 text-[11px] leading-relaxed text-ink-2">{review.error}</p>
        <p className="mt-1 text-[11px] text-ink-3">
          The setup above is the rules-based reading, which does not depend on
          a model.
        </p>
      </section>
    );
  }

  const verdict = review.verdict ? VERDICT[review.verdict] : null;
  const Icon = verdict?.icon ?? Bot;
  const rejected = review.levels_rejected ?? [];

  return (
    <section className={cn("rounded-lg border px-4 py-3",
                           verdict?.ring ?? "border-line bg-surface-1")}>
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h3 className={cn("flex items-center gap-2 text-xs font-semibold",
                          verdict?.tone ?? "text-ink-1")}>
          <Icon size={14} />
          AI review{verdict ? `: ${verdict.label}` : ""}
        </h3>
        {review.model && (
          <span className="num text-[10px] text-ink-3">{review.model}</span>
        )}
      </header>

      {review.reasoning && (
        <p className="mt-2 text-[11px] leading-relaxed text-ink-1">
          {review.reasoning}
        </p>
      )}

      {review.risks && (
        <p className="mt-2 flex gap-1.5 text-[11px] leading-relaxed text-ink-2">
          <TriangleAlert size={12} className="mt-0.5 shrink-0 text-warning" />
          <span>{review.risks}</span>
        </p>
      )}

      {rejected.length > 0 && (
        <div className="mt-3 rounded-md border border-loss/35 bg-loss/10 px-3 py-2">
          <p className="text-[11px] font-semibold text-loss">
            The model&apos;s own levels were discarded
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11px] text-loss/90">
            {rejected.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
          <p className="mt-1 text-[10px] text-ink-3">
            The entry, stop and target shown above are the rules&apos; own, not
            the model&apos;s.
          </p>
        </div>
      )}
    </section>
  );
}
