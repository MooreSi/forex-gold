import { useId, useState, type MouseEvent } from "react";
import { formatBrokerTime, formatMoney, formatSignedMoney, pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import type { CurvePoint } from "../hooks/useClosedTrades";
import { DD_H, M, tickLabel, type Geometry } from "./equityGeometry";

/**
 * The equity curve itself: the realised line over a gradient that is green
 * above zero and red below it, a drawdown strip underneath on the same time
 * axis, and a crosshair that reads off any trade.
 *
 * Both gradients are in user space and split AT the zero line, so a curve
 * that dips under zero turns red exactly where it crosses rather than the
 * whole line taking one colour from where the window happened to end.
 */
export function EquityCurveView({ geometry: g, points, up }: {
  geometry: Geometry;
  points: CurvePoint[];
  up: boolean;
}) {
  const [hover, setHover] = useState<number | null>(null);
  // Gradients are referenced by id; two charts on one page must not share one.
  const id = useId().replace(/[^a-zA-Z0-9-]/g, "");
  const { w, h } = g.box;
  const plotBottom = h - M.bottom;
  const zero = Math.min(1, Math.max(0, (g.zeroY - M.top) / (plotBottom - M.top)));

  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    setHover(g.indexAt(((e.clientX - rect.left) / rect.width) * w));
  };
  const hp = hover == null ? null : g.points[hover];
  const peak = g.points[g.peakIndex];
  const trough = g.troughIndex == null ? null : g.points[g.troughIndex];
  const showPeak = g.peakIndex > 0 && points[g.peakIndex].pnl > 0;
  const maxDd = trough && g.troughIndex != null
    ? Math.max(...points.slice(0, g.troughIndex + 1).map((p) => p.pnl))
      - points[g.troughIndex].pnl
    : 0;

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${w} ${h}`} width={w} height={h}
        role="img" aria-label="Realised profit and loss across the window"
        className="block w-full cursor-crosshair select-none"
        onMouseMove={onMove} onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id={`${id}-fill`} gradientUnits="userSpaceOnUse"
            x1="0" y1={M.top} x2="0" y2={plotBottom}>
            <stop offset="0" stopColor="var(--color-profit)" stopOpacity="0.38" />
            <stop offset={zero} stopColor="var(--color-profit)" stopOpacity="0.02" />
            <stop offset={zero} stopColor="var(--color-loss)" stopOpacity="0.02" />
            <stop offset="1" stopColor="var(--color-loss)" stopOpacity="0.38" />
          </linearGradient>
          <linearGradient id={`${id}-line`} gradientUnits="userSpaceOnUse"
            x1="0" y1={M.top} x2="0" y2={plotBottom}>
            <stop offset={zero} stopColor="var(--color-profit)" />
            <stop offset={zero} stopColor="var(--color-loss)" />
          </linearGradient>
        </defs>

        {g.yTicks.map((t) => (
          <g key={t.value}>
            <line x1={M.left} y1={t.y} x2={w - M.right} y2={t.y}
              stroke="currentColor" strokeWidth="1"
              strokeDasharray={t.value === 0 ? undefined : "2 4"}
              className={t.value === 0 ? "text-ink-3/60" : "text-line/70"} />
            <text data-testid={`curve-y-${t.value}`} x={w - M.right + 8} y={t.y + 3}
              className="num fill-ink-3 text-[10px]">
              {t.label}
            </text>
          </g>
        ))}
        {g.xTicks.map((t, i) => (
          <text key={`${t.label}-${i}`} data-testid={`curve-x-${i}`} x={t.x} y={h - 6}
            textAnchor={i === 0 ? "start" : i === g.xTicks.length - 1 ? "end" : "middle"}
            className="num fill-ink-3 text-[10px]">
            {t.label}
          </text>
        ))}

        <path data-testid="equity-area" d={g.area} fill={`url(#${id}-fill)`} stroke="none" />
        {/* A soft halo under the line: the same path, wide and faint. */}
        <path d={g.line} fill="none" stroke={`url(#${id}-line)`} strokeWidth="7"
          strokeOpacity="0.12" strokeLinejoin="round" strokeLinecap="round" />
        <path data-testid="equity-path" d={g.line} fill="none"
          stroke={`url(#${id}-line)`} strokeWidth="2" strokeLinejoin="round"
          strokeLinecap="round" className={up ? "text-profit" : "text-loss"} />

        {showPeak && (
          <g>
            <circle cx={peak.x} cy={peak.y} r="3.5" fill="var(--color-accent)"
              stroke="var(--color-surface-1)" strokeWidth="1.5" />
            <text x={peak.x} y={peak.y - 10}
              textAnchor={peak.x < M.left + 50 ? "start"
                : peak.x > w - M.right - 50 ? "end" : "middle"}
              className="num fill-accent text-[10px] font-semibold">
              Peak {formatMoney(points[g.peakIndex].pnl)}
            </text>
          </g>
        )}
        {trough && (
          <circle cx={trough.x} cy={trough.y} r="3.5" fill="var(--color-loss)"
            stroke="var(--color-surface-1)" strokeWidth="1.5">
            <title>Bottom of the worst drawdown</title>
          </circle>
        )}

        {g.last && hp == null && (
          <g className={up ? "text-profit" : "text-loss"}>
            <circle cx={g.last.x} cy={g.last.y} r="4" fill="currentColor" opacity="0.35">
              <animate attributeName="r" values="4;10;4" dur="2.4s" repeatCount="indefinite" />
              <animate attributeName="opacity" values="0.35;0;0.35" dur="2.4s"
                repeatCount="indefinite" />
            </circle>
            <circle cx={g.last.x} cy={g.last.y} r="3.5" fill="currentColor"
              stroke="var(--color-surface-1)" strokeWidth="1.5" />
          </g>
        )}

        {hp && hover != null && (
          <g pointerEvents="none">
            <line x1={hp.x} y1={M.top} x2={hp.x} y2={plotBottom}
              stroke="var(--color-ink-3)" strokeDasharray="3 3" />
            <line x1={M.left} y1={hp.y} x2={w - M.right} y2={hp.y}
              stroke="var(--color-ink-3)" strokeOpacity="0.4" strokeDasharray="3 3" />
            <circle cx={hp.x} cy={hp.y} r="4.5" fill="var(--color-surface-1)"
              stroke={points[hover].pnl >= 0 ? "var(--color-profit)" : "var(--color-loss)"}
              strokeWidth="2" />
          </g>
        )}
      </svg>

      {/* Drawdown: how far below its own best the curve is at each trade. */}
      <div className="mt-1 border-t border-line/60 pt-1.5">
        <p className="mb-1 text-[9px] uppercase tracking-wider text-ink-3">
          Drawdown from peak
        </p>
        <svg viewBox={`0 0 ${w} ${DD_H}`} width={w} height={DD_H} aria-hidden
          preserveAspectRatio="none" className="block w-full">
          {g.troughIndex != null && (
            <text x={w - M.right + 8} y={DD_H - 4} className="num fill-loss text-[10px]">
              {tickLabel(-maxDd)}
            </text>
          )}
          <path data-testid="drawdown-area" d={g.drawdown.area}
            fill="var(--color-loss)" fillOpacity="0.18" />
          <path d={g.drawdown.line} fill="none" stroke="var(--color-loss)"
            strokeOpacity="0.8" strokeWidth="1.25" vectorEffect="non-scaling-stroke" />
          {hp && <line x1={hp.x} y1="0" x2={hp.x} y2={DD_H}
            stroke="var(--color-ink-3)" strokeDasharray="3 3" />}
        </svg>
      </div>

      {hp && hover != null && (
        <div data-testid="curve-tooltip"
          className="pointer-events-none absolute top-2 z-10 min-w-36 rounded-md border
                     border-line bg-surface-1/95 px-2.5 py-1.5 text-[11px] shadow-lg
                     backdrop-blur"
          style={hp.x > w / 2
            ? { right: `${((w - hp.x) / w) * 100 + 1.5}%` }
            : { left: `${(hp.x / w) * 100 + 1.5}%` }}>
          <p className="num text-ink-3">{formatBrokerTime(points[hover].ts)}</p>
          <p className={cn("num text-sm font-semibold", pnlColour(points[hover].pnl))}>
            {formatMoney(points[hover].pnl)}
          </p>
          <p className="num text-ink-3">
            {hover === 0 ? "start of view" : (
              <>
                trade {hover}:{" "}
                <span className={pnlColour(points[hover].pnl - points[hover - 1].pnl)}>
                  {formatSignedMoney(points[hover].pnl - points[hover - 1].pnl)}
                </span>
              </>
            )}
          </p>
        </div>
      )}
    </div>
  );
}
