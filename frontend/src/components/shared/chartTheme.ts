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

/** A hex token at an alpha. Returns the input unchanged if it is not a hex. */
export function rgba(colour: string, alpha: number): string {
  const hex = colour.trim().replace("#", "");
  if (hex.length !== 6) return colour;
  const n = parseInt(hex, 16);
  if (!Number.isFinite(n)) return colour;
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
