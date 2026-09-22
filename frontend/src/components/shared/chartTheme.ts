/**
 * The chart's colours, read from the theme.
 *
 * lightweight-charts paints to a canvas and cannot use a CSS variable, so
 * every chart in this app has to be TOLD its colours and re-told them when
 * the theme changes. Shared because there are three charts now — the Chart
 * tab, the ORB report and Set & Forget — and a second copy is how one of them
 * ends up dark inside a white panel, which is exactly what the first one did
 * and what Set & Forget did again on 2026-09-21.
 *
 * In `shared/` rather than `chart/internal/`, which is where it started: a
 * domain's `internal/` is not for other domains to import, and the third
 * caller is what made that rule bite. Nothing about the module changed in the
 * move.
 */
export function token(name: string, fallback: string): string {
  if (typeof getComputedStyle !== "function") return fallback;
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  return value || fallback;
}

export function chartColours() {
  return {
    background: token("--color-surface-1", "#030712"),
    text: token("--color-ink-2", "#9ca3af"),
    grid: token("--color-surface-3", "#1b2333"),
    border: token("--color-line", "#263044"),
    profit: token("--color-profit", "#00cc88"),
    loss: token("--color-loss", "#ff4444"),
    accent: token("--color-accent", "#ffd700"),
    remote: token("--color-remote", "#64b4ff"),
  };
}

/**
 * A hex token at an alpha. Returns the input unchanged if it is not a hex.
 *
 * **All four hex spellings, not just the six-digit one.** The theme is
 * authored in six digits, but what reaches `getComputedStyle` is what the CSS
 * minifier emitted, and a minifier shortens `#00cc88` to `#0c8`. Accepting
 * only six digits therefore depended on whether a given colour happened to be
 * shortenable: the dark theme's profit green and loss red both are, and the
 * light theme's `#059669` and `#dc2626` are not. So every fair-value gap on
 * the chart — and every ORB session band — was a translucent tint in light
 * mode and a solid block painted over the candles in dark mode, from the same
 * token. Reported on 2026-09-22 and pinned by `chartTheme.test.ts`.
 *
 * An alpha carried by the token (the `-dim` variants are eight digits) is
 * dropped in favour of the requested one. The argument is what the caller
 * means; honouring the token's instead would make it a suggestion.
 */
export function rgba(colour: string, alpha: number): string {
  let hex = colour.trim().replace("#", "");
  // Shorthand first: `#rgb` and `#rgba` each double every digit.
  if (hex.length === 3 || hex.length === 4) {
    hex = hex.split("").map((d) => d + d).join("");
  }
  // Drop a trailing alpha pair; `alpha` is the one that applies.
  if (hex.length === 8) hex = hex.slice(0, 6);
  if (hex.length !== 6) return colour;
  // `parseInt` stops at the first character it cannot read, so "zzzzzz" is
  // NaN but "00zzzz" would be 0 — a silent black. Reject anything that is not
  // six hex digits before parsing it.
  if (!/^[0-9a-fA-F]{6}$/.test(hex)) return colour;
  const n = parseInt(hex, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/**
 * Re-render when the theme changes.
 *
 * The document attribute rather than `useTheme()`: a chart that throws because
 * a context is missing is a blank dashboard over a colour, and the theme is
 * the least important thing on it.
 */
export function watchTheme(onChange: () => void): () => void {
  if (typeof MutationObserver !== "function") return () => undefined;
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, {
    attributes: true, attributeFilter: ["data-theme"],
  });
  return () => observer.disconnect();
}
