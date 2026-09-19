import { describe, expect, it } from "vitest";
import { rectsFor, type Converters } from "../internal/fvgGeometry";

/**
 * The arithmetic behind the fair-value gap overlay.
 *
 * Tested apart from the chart because lightweight-charts owns its own canvas
 * and this is the part that can be wrong in a way nobody notices: a zone
 * plotted at the wrong price is a level an operator reads off the screen and
 * trades against.
 */
function converters(over: Partial<Converters> = {}): Converters {
  return {
    // Time 1000 -> x 0, and one pixel per second.
    timeToX: (ts) => (ts < 1000 || ts > 1600 ? null : ts - 1000),
    // Price 4000 -> y 500, price 4100 -> y 0. Higher price, smaller y.
    priceToY: (p) => (p < 4000 || p > 4100 ? null : (4100 - p) * 5),
    visibleTo: 1600,
    width: 600,
    height: 500,
    ...over,
  };
}

const zone = (over: Record<string, unknown> = {}) => ({
  ts: 1100, top: 4050, bottom: 4040, direction: "bullish", ...over,
});

describe("where a zone lands", () => {
  it("starts at the candle that created it", () => {
    const [rect] = rectsFor([zone()], converters());

    expect(rect!.x).toBe(100);
  });

  it("runs to the right edge, because the zone is still open", () => {
    // A gap is drawn forward from its candle to the present, not for its own
    // three-candle width: it means "price left an imbalance here and has not
    // come back through it".
    const [rect] = rectsFor([zone()], converters());

    expect(rect!.x + rect!.width).toBe(600);
  });

  it("puts the top of the band at the higher price", () => {
    const [rect] = rectsFor([zone({ top: 4050, bottom: 4040 })], converters());

    expect(rect!.y).toBe(250);
    expect(rect!.height).toBe(50);
  });

  it("does not care which way round top and bottom arrive", () => {
    const [rect] = rectsFor([zone({ top: 4040, bottom: 4050 })], converters());

    expect(rect!.y).toBe(250);
    expect(rect!.height).toBe(50);
  });

  it("keeps the direction so the band can be coloured", () => {
    const [rect] = rectsFor([zone({ direction: "bearish" })], converters());

    expect(rect!.direction).toBe("bearish");
  });
});

describe("zones that cannot be drawn", () => {
  it("drops a zone whose prices are off the visible range", () => {
    // Drawing it at y=0 would put a band across the top of the chart at a
    // price that is not on the chart.
    expect(rectsFor([zone({ top: 4200, bottom: 4190 })], converters())).toEqual([]);
  });

  it("drops a zone that has not happened on screen yet", () => {
    expect(rectsFor([zone({ ts: 5000 })], converters())).toEqual([]);
  });

  it("draws a scrolled-off zone from the left edge", () => {
    // Scrolled off is not cancelled: the gap still applies to everything on
    // screen, so it is drawn from the edge rather than dropped.
    const [rect] = rectsFor([zone({ ts: 10 })], converters());

    expect(rect!.x).toBe(0);
    expect(rect!.width).toBe(600);
  });

  it("gives a paper-thin zone at least one pixel", () => {
    // Zero height draws nothing, which reads as "there is no gap here".
    const [rect] = rectsFor([zone({ top: 4050, bottom: 4050 })], converters());

    expect(rect!.height).toBe(1);
  });

  it("draws nothing at all before the chart has a size", () => {
    expect(rectsFor([zone()], converters({ width: 0 }))).toEqual([]);
  });

  it("survives a missing zone list", () => {
    expect(rectsFor(undefined as never, converters())).toEqual([]);
  });
});

describe("several zones", () => {
  it("keeps every drawable one", () => {
    const rects = rectsFor(
      [zone({ ts: 1100 }), zone({ ts: 1200 }), zone({ ts: 5000 })],
      converters(),
    );

    expect(rects.map((r) => r.x)).toEqual([100, 200]);
  });
});
