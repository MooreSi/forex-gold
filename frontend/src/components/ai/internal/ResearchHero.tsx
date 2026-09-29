import { useId } from "react";
import { Newspaper, Sparkles } from "lucide-react";
import type { MarketAnalysis } from "../hooks/useMarketResearch";

/** One ingot: a lit top face over a darker front face. */
function Ingot({ x, y, fill }: { x: number; y: number; fill: string }) {
  return (
    <g transform={`translate(${x} ${y})`}>
      <path d="M8 0 H52 L60 10 H0 Z" fill={fill} opacity={0.95} />
      <path d="M0 10 H60 V24 H0 Z" fill={fill} opacity={0.7} />
      <path d="M12 3 H40" stroke="white" strokeOpacity={0.45} strokeWidth={1.5}
        strokeLinecap="round" />
    </g>
  );
}

/** Three stacked bars. Pure decoration: nothing on it is data. */
function IngotStack() {
  const g = useId();
  return (
    <svg aria-hidden viewBox="0 0 130 64" className="h-14 w-28 shrink-0 drop-shadow-md">
      <defs>
        <linearGradient id={g} x1="0" x2="1" y1="0" y2="1">
          <stop offset="0%" style={{ stopColor: "var(--color-accent)" }} />
          <stop offset="100%" style={{ stopColor: "var(--color-warning)" }} />
        </linearGradient>
      </defs>
      <Ingot x={4} y={38} fill={`url(#${g})`} />
      <Ingot x={66} y={38} fill={`url(#${g})`} />
      <Ingot x={35} y={12} fill={`url(#${g})`} />
    </svg>
  );
}

/**
 * The banner over the analysis: which market, and what went into the answer.
 *
 * Styled as the dashboard's price card is (the "Au" roundel, the faint gold
 * wash), so the two pages read as one product.
 */
export function ResearchHero({ analysis, savedAt }: {
  analysis: MarketAnalysis; savedAt: string;
}) {
  return (
    <section
      data-testid="research-hero"
      className="relative overflow-hidden rounded-lg border border-line bg-surface-2 px-4 py-3"
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-gradient-to-r
                   from-accent/15 via-accent/5 to-transparent"
      />
      {/* A faint dot grid, the texture of chart paper. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.15]"
        style={{
          backgroundImage: "radial-gradient(var(--color-ink-3) 1px, transparent 1px)",
          backgroundSize: "14px 14px",
          maskImage: "linear-gradient(to left, black, transparent 70%)",
        }}
      />
      <div className="relative flex items-center justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <span
            aria-hidden
            className="flex size-10 shrink-0 items-center justify-center rounded-full
                       bg-gradient-to-br from-accent to-warning text-[15px]
                       font-bold text-surface-0 shadow"
          >
            Au
          </span>
          <div className="min-w-0">
            <p className="text-sm font-semibold leading-tight text-ink-1">
              XAUUSD <span className="font-normal text-ink-3">Gold / US Dollar</span>
            </p>
            <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-ink-3">
              <span className="inline-flex items-center gap-1">
                <Sparkles size={11} className="text-accent" aria-hidden />
                AI market briefing
              </span>
              {savedAt && <span>{savedAt}</span>}
              {analysis.fetched_news && (
                <span className="inline-flex items-center gap-1">
                  <Newspaper size={11} aria-hidden />
                  {analysis.news_count ?? 0} headlines read
                </span>
              )}
            </p>
          </div>
        </div>
        <IngotStack />
      </div>
    </section>
  );
}
