import type { FvgZone } from "@/api/types";

/**
 * Where to draw a fair-value gap, in pixels.
 *
 * Separated from the drawing because lightweight-charts has no rectangle
 * primitive — the zones are an SVG layer over its canvas — and because the
 * arithmetic is the part that can be wrong in a way nobody notices. A zone
 * plotted at the wrong price is a level an operator reads off the screen.
 *
 * A gap is drawn from the candle that created it **forward to the right edge**,
 * because that is what the zone means: price left an imbalance here and has not
 * come back through it. The right edge is the present, not the zone's own
 * width.
 */
export interface Converters {
  /** Chart x for a unix-second timestamp, or null when it is off-screen. */
  timeToX: (ts: number) => number | null;
  /** Chart y for a price, or null when it is outside the visible range. */
  priceToY: (price: number) => number | null;
  /** The rightmost visible timestamp.
   *
   *  Needed because `timeToX` answers null for BOTH "scrolled off the left"
   *  and "not on screen yet", and those want opposite treatment: the first is
   *  drawn from the edge, the second is not drawn at all. The coordinate alone
   *  cannot tell them apart. */
  visibleTo: number;
  width: number;
  height: number;
}

export interface FvgRect {
  ts: number;
  direction: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Zones with no usable geometry are dropped, never clamped into existence. */
export function rectsFor(zones: FvgZone[], c: Converters): FvgRect[] {
  // No explicit size guard: a chart with no width gives every zone a
  // non-positive width, which the check further down already drops. Mutation
  // testing found the guard unreachable (2026-09-19) and an unreachable guard
  // is a claim the tests cannot check.
  const out: FvgRect[] = [];

  for (const zone of zones ?? []) {
    const yTop = c.priceToY(Math.max(zone.top, zone.bottom));
    const yBottom = c.priceToY(Math.min(zone.top, zone.bottom));
    // A price outside the visible range has no coordinate. Drawing it at 0
    // would put a band across the top of the chart at a price that is not
    // there.
    if (yTop == null || yBottom == null) continue;

    // A zone beyond the right edge has not happened on screen yet.
    if (zone.ts > c.visibleTo) continue;
    // A zone that starts before the left edge still applies: it is scrolled
    // off, not cancelled, so it is drawn from the edge.
    const rawX = c.timeToX(zone.ts);
    const x = rawX == null ? 0 : Math.max(0, rawX);

    const width = c.width - x;
    if (width <= 0) continue;

    const height = Math.abs(yBottom - yTop);
    out.push({
      ts: zone.ts,
      direction: zone.direction,
      x,
      // `yTop` is the coordinate of the HIGHER price, and screen y decreases
      // as price rises, so this is already the top of the band. A
      // Math.min(yTop, yBottom) here was unreachable for the same reason.
      y: yTop,
      width,
      // A zone thinner than a pixel is still a zone. Zero height draws
      // nothing at all, which reads as "there is no gap here".
      height: Math.max(height, 1),
    });
  }
  return out;
}
