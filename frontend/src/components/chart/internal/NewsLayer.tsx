import { useEffect, useMemo, useState } from "react";
import type { IChartApi, MouseEventParams, Time, UTCTimestamp } from "lightweight-charts";
import type { Candle } from "@/api/types";
import { rgba, token } from "@/components/shared/chartTheme";
import { formatPrice, formatUtcTime } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import type { ChartNewsLayer } from "../hooks/useChartNews";
import { barSeconds, moveAfter, placeNews, type NewsImpact } from "./newsMarks";

interface NewsLayerProps {
  chart: IChartApi;
  isDisposed: () => boolean;
  bars: Candle[];
  /** Seconds per bar; read off the bars when the caller does not say. */
  tfSeconds?: number;
  news: ChartNewsLayer;
  themeTick: number;
}

const LETTER: Record<NewsImpact, string> = { high: "H", medium: "M", low: "L" };

// The ForexFactory convention the owner reads the calendar in: red, orange,
// yellow. Through the theme tokens, so a light panel still shows them.
function impactColours(): Record<NewsImpact, string> {
  return {
    high: token("--color-loss", "#ff4444"),
    medium: token("--color-warning", "#ffb020"),
    low: token("--color-accent", "#ffd700"),
  };
}

/**
 * The NEWS button's marks over the Broker chart: a tinted column on each bar
 * a release fell in, a letter for its strongest impact, and on hover the
 * releases with how price moved in the half hour after.
 *
 * An SVG over the canvas like the FVG and drawing layers, and like them
 * re-projected on every pan and zoom. It never takes the mouse; the hover
 * comes from the chart's own crosshair.
 */
export function NewsLayer({ chart, isDisposed, bars, tfSeconds: given, news, themeTick }: NewsLayerProps) {
  const [, setFrame] = useState(0);
  const [hovered, setHovered] = useState<number | null>(null);
  const times = useMemo(() => bars.map((b) => b.ts), [bars]);
  const tfSeconds = given ?? barSeconds(times);
  const marks = useMemo(
    () => placeNews(news.events, times, tfSeconds, news.correctionS, news.filter),
    [news, times, tfSeconds],
  );
  const colours = useMemo(() => impactColours(), [themeTick]);

  useEffect(() => {
    const redraw = () => setFrame((n) => n + 1);
    const onMove = (p: MouseEventParams<Time>) => setHovered(typeof p.time === "number" ? p.time : null);
    const scale = chart.timeScale();
    scale.subscribeVisibleLogicalRangeChange(redraw);
    chart.subscribeCrosshairMove(onMove);
    return () => {
      if (isDisposed()) return;
      scale.unsubscribeVisibleLogicalRangeChange(redraw);
      chart.unsubscribeCrosshairMove(onMove);
    };
  }, [chart, isDisposed]);

  if (isDisposed() || marks.length === 0) return null;
  const scale = chart.timeScale();
  const spacing = Math.max(2, Number(scale.options().barSpacing) || 6);
  const placed = marks
    .map((m) => ({ m, x: scale.timeToCoordinate(m.barTime as UTCTimestamp) as number | null }))
    .filter((p): p is { m: typeof p.m; x: number } => p.x !== null);
  const hover = placed.find((p) => p.m.barTime === hovered);
  const move = hover ? moveAfter(bars, hover.m.barTime, tfSeconds) : null;

  return (
    <>
      <svg
        data-testid="news-layer"
        className="pointer-events-none absolute inset-0 z-10 h-full w-full"
        aria-hidden
      >
        {placed.map(({ m, x }) => (
          <g key={m.barTime} data-testid={`news-mark-${m.impact}-${m.barTime}`}>
            <rect
              x={x - spacing / 2} y={0} width={spacing} height="100%"
              fill={rgba(colours[m.impact], m.barTime === hovered ? 0.28 : 0.14)}
            />
            <line
              x1={x} x2={x} y1={0} y2="100%"
              stroke={rgba(colours[m.impact], 0.6)} strokeWidth="1" strokeDasharray="3 3"
            />
            <rect x={x - 7} y={2} width={14} height={13} rx={2} fill={colours[m.impact]} />
            <text
              x={x} y={9} textAnchor="middle" dominantBaseline="middle"
              fontSize={9} fontWeight="700" fill="#000"
            >
              {m.events.length > 1 ? `${LETTER[m.impact]}${m.events.length}` : LETTER[m.impact]}
            </text>
          </g>
        ))}
      </svg>
      {hover && (
        <div
          data-testid="news-hover"
          className="pointer-events-none absolute top-5 z-30 w-64 rounded border border-line bg-surface-1/95 p-2 text-[11px] shadow"
          style={hover.x > chart.timeScale().width() / 2
            ? { right: `calc(100% - ${hover.x - 10}px)` }
            : { left: hover.x + 10 }}
        >
          <div className="mb-1 text-ink-3">{formatUtcTime(hover.m.events[0].ts)} UTC</div>
          <ul className="space-y-0.5">
            {hover.m.events.map((e) => (
              <li key={`${e.ts}-${e.currency}-${e.title}`} className="flex gap-1.5">
                <span className="num font-semibold" style={{ color: colours[e.impact as NewsImpact] }}>
                  {LETTER[e.impact as NewsImpact] ?? "?"}
                </span>
                <span className="num text-ink-3">{e.currency}</span>
                <span className="text-ink-1">{e.title}</span>
                {(e.forecast || e.previous) && (
                  <span className="ml-auto whitespace-nowrap text-ink-3">
                    {e.forecast ? `f ${e.forecast}` : ""}{e.forecast && e.previous ? " · " : ""}
                    {e.previous ? `p ${e.previous}` : ""}
                  </span>
                )}
              </li>
            ))}
          </ul>
          {move && (
            <div className="mt-1.5 border-t border-line pt-1 text-ink-2">
              Next {move.bars === 1 ? "bar" : `${move.bars} bars`}:{" "}
              <span className={cn("num", move.change >= 0 ? "text-profit" : "text-loss")}>
                {move.change >= 0 ? "+" : ""}{formatPrice(move.change)}
              </span>
              {" "}· range <span className="num">{formatPrice(move.range)}</span>
            </div>
          )}
        </div>
      )}
    </>
  );
}
