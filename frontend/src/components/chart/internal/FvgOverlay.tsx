import { useMemo } from "react";
import { rgba, token } from "@/components/shared/chartTheme";
import type { FvgRect } from "./fvgGeometry";

// A fair-value gap is an imbalance price left behind. Bullish gaps sit below
// price and bearish above, so they take the same profit/loss meaning the rest
// of the app uses -- through the THEME tokens, not fixed hex. #00cc88 at 13%
// over a white panel is invisible, which is what the first version of this
// overlay was in light mode: six correctly positioned zones nobody could see.
function fvgColours() {
  const profit = token("--color-profit", "#00cc88");
  const loss = token("--color-loss", "#ff4444");
  return {
    fill: { bullish: rgba(profit, 0.18), bearish: rgba(loss, 0.18) },
    edge: { bullish: rgba(profit, 0.55), bearish: rgba(loss, 0.55) },
    // The label has to carry on its own over a 18%-opacity band, so it is far
    // more opaque than the fill it sits on.
    label: { bullish: rgba(profit, 0.95), bearish: rgba(loss, 0.95) },
  };
}

/** Pixels in from the zone's left edge, so the text clears its own border. */
const FVG_LABEL_INSET = 6;
const FVG_LABEL_SIZE = 10;

/**
 * The fair-value-gap zones over the Broker chart. Moved verbatim out of
 * CandleChart (2026-09-28) when the drawing layer pushed that file past its
 * size budget; the positions come from `fvgGeometry.rectsFor`.
 */
export function FvgOverlay({ rects, themeTick }: { rects: FvgRect[]; themeTick: number }) {
  // Recomputed with the theme, like the chart's own colours.
  const fvgPaint = useMemo(() => fvgColours(), [themeTick]);
  if (rects.length === 0) return null;
  return (
    <svg
      data-testid="fvg-overlay"
      // z-10, not just "after the canvas in the DOM". lightweight-charts
      // gives its own canvases explicit z-index 1 and 2, so an overlay at
      // `auto` is painted UNDER them: six correctly positioned zones,
      // present in the DOM, invisible on screen. Found by inspecting the
      // running app on 2026-09-19.
      className="pointer-events-none absolute inset-0 z-10 h-full w-full"
      aria-hidden
    >
      {rects.map((r) => (
        <g key={`${r.ts}-${r.y}`}>
          <rect
            data-testid={`fvg-${r.direction}-${r.ts}`}
            x={r.x} y={r.y} width={r.width} height={r.height}
            fill={fvgPaint.fill[r.direction as "bullish" | "bearish"]
              ?? "rgba(156,163,175,0.14)"}
            stroke={fvgPaint.edge[r.direction as "bullish" | "bearish"]
              ?? "rgba(156,163,175,0.4)"}
            strokeWidth="0.5"
          />
          {/* The band's colour alone does not say what the band IS. The
              label is drawn at the zone's own left edge and vertical
              centre, so a thin gap still gets named rather than silently
              losing its label. */}
          <text
            data-testid={`fvg-label-${r.direction}-${r.ts}`}
            x={r.x + FVG_LABEL_INSET}
            y={r.y + r.height / 2}
            dominantBaseline="middle"
            fontSize={FVG_LABEL_SIZE}
            fontWeight="600"
            letterSpacing="0.5"
            fill={fvgPaint.label[r.direction as "bullish" | "bearish"]
              ?? "rgba(156,163,175,0.9)"}
          >
            FVG
          </text>
        </g>
      ))}
    </svg>
  );
}
