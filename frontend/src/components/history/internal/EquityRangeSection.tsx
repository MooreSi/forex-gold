import { useMemo } from "react";
import { formatBrokerTime } from "@/components/shared/format";
import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import type { CurvePoint } from "../hooks/useClosedTrades";

/**
 * The time-window slider under the equity curve (owner, 2026-10-09).
 *
 * Two native range inputs laid over one track: keyboard, screen readers and
 * touch all work without a slider library, and there is none in this app.
 * Behind them is the WHOLE window's curve, drawn small against time, so the
 * handles are placed against the shape rather than against blank track.
 *
 * It narrows what is drawn inside the window already loaded; the 7/30/90/365
 * buttons in the panel header decide what is loaded.
 */
const PRESETS = [
  { label: "1D", secs: 86_400 },
  { label: "1W", secs: 7 * 86_400 },
  { label: "1M", secs: 30 * 86_400 },
];

const NAV_W = 1000;
const NAV_H = 36;

const THUMB = cn(
  "pointer-events-none absolute inset-0 h-full w-full appearance-none bg-transparent",
  "[&::-webkit-slider-thumb]:pointer-events-auto [&::-webkit-slider-thumb]:h-9",
  "[&::-webkit-slider-thumb]:w-2.5 [&::-webkit-slider-thumb]:cursor-ew-resize",
  "[&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-sm",
  "[&::-webkit-slider-thumb]:border [&::-webkit-slider-thumb]:border-accent",
  "[&::-webkit-slider-thumb]:bg-surface-1",
  "[&::-moz-range-thumb]:pointer-events-auto [&::-moz-range-thumb]:h-9",
  "[&::-moz-range-thumb]:w-2.5 [&::-moz-range-thumb]:cursor-ew-resize",
  "[&::-moz-range-thumb]:rounded-sm [&::-moz-range-thumb]:border",
  "[&::-moz-range-thumb]:border-accent [&::-moz-range-thumb]:bg-surface-1",
  "focus-visible:outline-none [&:focus-visible::-webkit-slider-thumb]:ring-2",
  "[&:focus-visible::-webkit-slider-thumb]:ring-accent/60",
);

export function EquityRangeSection({ points, min, max, from, to, onChange }: {
  points: CurvePoint[];
  min: number;
  max: number;
  from: number;
  to: number;
  onChange: (from: number, to: number) => void;
}) {
  const span = max - min || 1;
  const pct = (ts: number) => ((ts - min) / span) * 100;

  const nav = useMemo(() => {
    const values = points.map((p) => p.pnl);
    const lo = Math.min(0, ...values);
    const hi = Math.max(0, ...values);
    const yr = hi - lo || 1;
    const line = points.map((p, i) =>
      `${i === 0 ? "M" : "L"}${(((p.ts - min) / span) * NAV_W).toFixed(1)},`
      + `${(NAV_H - 3 - ((p.pnl - lo) / yr) * (NAV_H - 6)).toFixed(1)}`).join(" ");
    return { line, area: `${line} L${NAV_W},${NAV_H} L0,${NAV_H} Z` };
  }, [points, min, span]);

  const whole = from <= min && to >= max;

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
        <span className="num text-ink-3">
          <span className="text-ink-1">{formatBrokerTime(from)}</span>
          {"  to  "}
          <span className="text-ink-1">{formatBrokerTime(to)}</span>
        </span>
        <div className="flex gap-1">
          {PRESETS.filter((p) => p.secs < span).map((p) => {
            const active = to >= max && Math.abs(max - from - p.secs) < 1;
            return (
              <button key={p.label} type="button" aria-pressed={active}
                onClick={() => onChange(max - p.secs, max)}
                className={cn("num rounded px-2 py-0.5 transition-colors",
                  active ? "bg-surface-3 text-ink-1"
                    : "text-ink-3 hover:bg-surface-2 hover:text-ink-2")}>
                {p.label}
              </button>
            );
          })}
          <button type="button" aria-pressed={whole} onClick={() => onChange(min, max)}
            className={cn("rounded px-2 py-0.5 transition-colors",
              whole ? "bg-surface-3 text-ink-1"
                : "text-ink-3 hover:bg-surface-2 hover:text-ink-2")}>
            All
          </button>
        </div>
      </div>

      <div className="relative h-9 overflow-hidden rounded-md border border-line bg-surface-2">
        <svg viewBox={`0 0 ${NAV_W} ${NAV_H}`} preserveAspectRatio="none" aria-hidden
          className="absolute inset-0 h-full w-full text-ink-3">
          <path d={nav.area} fill="currentColor" fillOpacity="0.12" />
          <path d={nav.line} fill="none" stroke="currentColor" strokeWidth="1.25"
            vectorEffect="non-scaling-stroke" />
        </svg>
        {/* Dim what is outside the window; the selected stretch stays clear. */}
        <div aria-hidden className="absolute inset-y-0 left-0 bg-surface-0/60"
          style={{ width: `${pct(from)}%` }} />
        <div aria-hidden className="absolute inset-y-0 right-0 bg-surface-0/60"
          style={{ width: `${100 - pct(to)}%` }} />
        <div aria-hidden className="absolute inset-y-0 border-y-2 border-accent/50"
          style={{ left: `${pct(from)}%`, right: `${100 - pct(to)}%` }} />

        <Tooltip label="Start of the view. Drag right to leave earlier trades out; the figures above are re-measured from here.">
          <input type="range" aria-label="Window start" min={min} max={max} step={1}
            value={from} className={THUMB}
            onChange={(e) => onChange(Math.min(Number(e.target.value), to), to)} />
        </Tooltip>
        <Tooltip label="End of the view. Drag left to leave later trades out.">
          <input type="range" aria-label="Window end" min={min} max={max} step={1}
            value={to} className={THUMB}
            onChange={(e) => onChange(from, Math.max(Number(e.target.value), from))} />
        </Tooltip>
      </div>
    </div>
  );
}
