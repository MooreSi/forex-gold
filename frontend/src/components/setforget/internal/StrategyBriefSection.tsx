import { useState } from "react";
import {
  BookOpen, ChevronDown, Crosshair, Layers, Ruler, ShieldCheck, Target, TrendingUp,
} from "lucide-react";
import { cn } from "@/lib/cn";

/**
 * What the method is, for someone who has not read the course.
 *
 * Open by default the first time and collapsible after that: a section whose
 * whole premise is "you do not need to watch the screen" is worth reading once
 * and worth getting out of the way afterwards.
 *
 * The rules below are reconstructed from Alex G's free material, the G-Club
 * community checklist and third-party write-ups — the full course is paid, and
 * nothing here is copied from it. Where the public record is thin, the section
 * says so rather than inventing a number: the indicator set in particular is
 * the confluence list the community uses, not a rule anyone has published.
 */
const STEPS = [
  {
    icon: Layers,
    title: "Top-down, three timeframes",
    body: "Weekly and Daily provide the main bias; the 4H refines structure "
      + "and levels. The app watches both directions and the model checks whether "
      + "a proposal agrees with the higher timeframes. Confirmation comes from the 30m.",
  },
  {
    icon: TrendingUp,
    title: "Read the structure, not an indicator",
    body: "Higher highs and higher lows is an uptrend; lower lows and lower "
      + "highs is a downtrend. Anything else is a range, and a range has no "
      + "direction to trade with.",
  },
  {
    icon: Crosshair,
    title: "Enter only at an Area of Interest",
    body: "A former swing point or consolidation acting as supply or demand. "
      + "Price visits the zone, then a closed 30m shift or engulfing confirms "
      + "the reaction. A recent wick test can qualify even when confirmation "
      + "closes just outside. A distant move is left alone.",
  },
  {
    icon: ShieldCheck,
    title: "The stop is structural",
    body: "Beyond the zone, or beyond the tail of the confirmation candle when "
      + "one has closed. Never a round number of points: the stop is where the "
      + "idea is wrong, not where the loss is comfortable.",
  },
  {
    icon: Target,
    title: "Target the next opposing zone",
    body: "The opposing Area of Interest limits the available reward. If none "
      + "exists, this app uses a 3R projection and labels it as such. An "
      + "intervening zone is never skipped to improve the ratio.",
  },
  {
    icon: Ruler,
    title: "1:2 minimum, configured risk",
    body: "Under 1:2 there is no trade however good the chart looks. Position "
      + "size comes from the stop distance and a fixed percentage of the "
      + "account when risk sizing is selected. A fixed lot size changes the cash "
      + "at risk with the stop distance; check that amount before placing.",
  },
];

export function StrategyBriefSection() {
  const [open, setOpen] = useState(true);

  return (
    <section className="rounded-lg border border-line bg-surface-2/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-3 text-left"
      >
        <BookOpen size={15} className="shrink-0 text-accent" />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold text-ink-1">
            The Set &amp; Forget method
          </span>
          <span className="block text-[11px] text-ink-3">
            Alex G&apos;s swing approach — plan it, place it, leave it alone
          </span>
        </span>
        <ChevronDown
          size={15}
          className={cn("shrink-0 text-ink-3 transition-transform",
                        open && "rotate-180")}
        />
      </button>

      {open && (
        <div className="border-t border-line px-4 pb-4 pt-3">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {STEPS.map(({ icon: Icon, title, body }, i) => (
              <article
                key={title}
                className="rounded-md border border-line bg-surface-1 p-3"
              >
                <h4 className="flex items-center gap-2 text-xs font-semibold text-ink-1">
                  <span className="num flex h-5 w-5 shrink-0 items-center justify-center
                                   rounded-full bg-surface-3 text-[10px] text-ink-2">
                    {i + 1}
                  </span>
                  <Icon size={13} className="shrink-0 text-accent" />
                  <span className="min-w-0">{title}</span>
                </h4>
                <p className="mt-1.5 text-[11px] leading-relaxed text-ink-2">{body}</p>
              </article>
            ))}
          </div>

          <p className="mt-3 rounded-md border border-line bg-surface-1 px-3 py-2
                        text-[10px] leading-relaxed text-ink-3">
            Reconstructed from Alex G&apos;s free material, the G-Club
            confluence checklist and third-party write-ups. The full course is
            paid and nothing here is taken from it. The indicator set — EMA
            50/200, the Fibonacci band and RSI — is the community&apos;s
            confluence list rather than a published rule, so it is scored as
            supporting evidence and never as the reason for a trade.
          </p>
        </div>
      )}
    </section>
  );
}
