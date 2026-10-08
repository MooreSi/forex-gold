import { useMemo } from "react";
import { formatMoney } from "@/components/shared/format";
import type { Period } from "./compoundMath";
import { useElementWidth } from "../hooks/useElementWidth";
import { BAR_BOX, buildBars } from "./compoundGeometry";

/**
 * Profit per month, or per week: a bar each. With losing days in the plan it
 * is the NET, and a losing period hangs below the zero line in the loss colour.
 *
 * Two of these sit side by side (owner, 2026-09-29), so the gradient id is
 * per chart: an id is global to the page, and two charts sharing one would
 * both paint with whichever definition came first.
 */
export function CompoundProfitBarsSection({ periods, noun, net }: {
  periods: Period[];
  noun: "Month" | "Week";
  /** True when the plan has losing days: the bars and the title are net. */
  net: boolean;
}) {
  const [frame, width] = useElementWidth<HTMLDivElement>();
  const chart = useMemo(
    () => buildBars(periods.map((p) => p.net), noun, { w: width ?? BAR_BOX.w, h: BAR_BOX.h }),
    [periods, noun, width]);
  const box = chart.box;
  const fillId = `cc-bar-${noun}`;
  const lossId = `cc-bar-loss-${noun}`;
  const title = `${net ? "Net profit" : "Profit"} per ${noun.toLowerCase()}`;

  return (
    <div ref={frame} className="min-w-0 space-y-1.5">
      <p className="text-[11px] uppercase tracking-wide text-ink-3">{title}</p>
      <svg viewBox={`0 0 ${box.w} ${box.h}`} width={box.w} height={box.h}
        role="img" aria-label={`Projected ${title.toLowerCase()}`}
        className="block w-full rounded border border-line bg-surface-2">
        <defs>
          <linearGradient id={fillId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-profit)" stopOpacity="0.95" />
            <stop offset="100%" stopColor="var(--color-profit)" stopOpacity="0.35" />
          </linearGradient>
          <linearGradient id={lossId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-loss)" stopOpacity="0.35" />
            <stop offset="100%" stopColor="var(--color-loss)" stopOpacity="0.95" />
          </linearGradient>
        </defs>
        {chart.yTicks.map((t) => (
          <g key={t.value}>
            <line x1={chart.plot.left} y1={t.y} x2={chart.plot.right} y2={t.y} stroke="currentColor"
              strokeDasharray={t.value === 0 ? undefined : "3 3"}
              className={t.value === 0 ? "text-ink-3/50" : "text-line"} />
            <text x={chart.plot.right + 6} y={t.y + 3} className="fill-ink-3 text-[10px]">{t.label}</text>
          </g>
        ))}
        {chart.bars.map((b) => (
          <rect key={b.label} x={b.x} y={b.y} width={b.w} height={Math.max(b.h, 0.5)} rx="1.5"
            data-sign={b.value < 0 ? "loss" : "profit"}
            fill={`url(#${b.value < 0 ? lossId : fillId})`} className="transition-opacity hover:opacity-70">
            <title>{`${b.label}: ${formatMoney(b.value)}`}</title>
          </rect>
        ))}
      </svg>
    </div>
  );
}
