/**
 * `rgba()` turns a theme token into a canvas colour at an alpha.
 *
 * It is the whole of the app's translucency on a canvas: the chart's
 * fair-value gaps, and the ORB report's session bands. lightweight-charts
 * paints to a canvas and cannot read a CSS variable, so a token that this
 * function cannot parse is not a slightly-wrong colour — it is returned
 * unchanged, which means fully opaque, which means a band that was meant to
 * tint the candles paints over them instead.
 *
 * **That is not hypothetical and it is why the shorthand cases are here.**
 * The dark theme's `--color-profit` is written `#00cc88` and the CSS minifier
 * emits it as `#0c8`; the light theme's `#059669` cannot be shortened, so it
 * survives as six digits. The length check only accepted six, so every
 * fair-value gap on the dashboard was a solid block in dark mode and a faint
 * tint in light mode, from the same source colour. Seen in the running app on
 * 2026-09-22.
 */
import { describe, expect, it } from "vitest";
import { rgba } from "../chartTheme";

describe("rgba", () => {
  it("expands the three-digit shorthand a CSS minifier produces", () => {
    // `#0c8` IS `#00cc88` — the dark theme's profit green, as the built
    // stylesheet actually carries it.
    expect(rgba("#0c8", 0.18)).toBe("rgba(0,204,136,0.18)");
    expect(rgba("#f44", 0.18)).toBe("rgba(255,68,68,0.18)");
  });

  it("gives the shorthand and the long form the same answer", () => {
    // The two spellings are the same colour. A chart that looked different in
    // dark mode than in light because of which one the minifier chose is the
    // bug this pins.
    expect(rgba("#0c8", 0.55)).toBe(rgba("#00cc88", 0.55));
    expect(rgba("#f44", 0.55)).toBe(rgba("#ff4444", 0.55));
  });

  it("still handles the six-digit form", () => {
    expect(rgba("#059669", 0.18)).toBe("rgba(5,150,105,0.18)");
    expect(rgba("  #dc2626  ", 0.4)).toBe("rgba(220,38,38,0.4)");
  });

  it("takes the requested alpha over one carried by the token", () => {
    // `--color-profit-dim` is `#00cc8833`. The caller asking for 0.18 means
    // 0.18; silently honouring the token's own 0x33 would make the alpha
    // argument a suggestion.
    expect(rgba("#00cc8833", 0.18)).toBe("rgba(0,204,136,0.18)");
    expect(rgba("#0c83", 0.18)).toBe("rgba(0,204,136,0.18)");
  });

  it("returns anything it cannot parse unchanged", () => {
    // A colour function is a legitimate CSS colour and a canvas can paint it.
    // Mangling it into a wrong rgba would be worse than declining to help.
    expect(rgba("oklch(0.7 0.15 160)", 0.2)).toBe("oklch(0.7 0.15 160)");
    expect(rgba("red", 0.2)).toBe("red");
    expect(rgba("#12345", 0.2)).toBe("#12345");
    expect(rgba("#zzzzzz", 0.2)).toBe("#zzzzzz");
    expect(rgba("", 0.2)).toBe("");
  });
});
