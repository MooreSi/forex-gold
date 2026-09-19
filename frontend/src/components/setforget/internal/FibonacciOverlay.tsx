import { formatPrice } from "@/components/shared/format";
import type { FibLine } from "../hooks/useChartGeometry";

/**
 * How far apart two labels have to be to both be readable, in pixels.
 *
 * A short leg on a wide price scale puts all four levels within twenty pixels
 * of each other, and four 9px labels stacked in that space are one illegible
 * smudge -- worse than three, because the one number a person wanted is in
 * there somewhere.
 */
const MIN_LABEL_GAP = 12;

/**
 * Which levels get a label. Every level still gets its LINE: the lines are the
 * reading, and the labels are the convenience.
 *
 * Sorted by screen position rather than by ratio, because a short's leg runs
 * high to low -- its 78.6% sits ABOVE its 38.2%, so a sweep in ratio order
 * would compare each label against one that is nowhere near it.
 */
function readable(fibs: FibLine[]): FibLine[] {
  const out: FibLine[] = [];
  let lastY = -Infinity;
  for (const f of [...fibs].sort((a, b) => a.y - b.y)) {
    if (f.y - lastY >= MIN_LABEL_GAP) {
      out.push(f);
      lastY = f.y;
    }
  }
  return out;
}

/**
 * The Fibonacci retracement over the last completed leg.
 *
 * **Pure.** Every price was computed by the backend's
 * `confluence.retracement_price`, which is the exact inverse of the function
 * the checklist scored the pullback with. Re-deriving them here would be a
 * second answer to where 61.8% is, visible as a band that disagrees with the
 * percentage printed next to it.
 *
 * The first and last lines are the edges of the band the checklist actually
 * scores (38.2% and 78.6%), so the shaded area is not decoration: it is the
 * zone a pullback has to land in. The two lines between them are the ones a
 * trader reads the pullback against, and are drawn but never scored.
 */
export function FibonacciOverlay({ fibs }: { fibs: FibLine[] }) {
  if (fibs.length === 0) return null;

  const ys = fibs.map((f) => f.y);
  const top = Math.min(...ys);
  const height = Math.max(...ys) - top;
  const labels = new Set(readable(fibs).map((f) => f.ratio));

  return (
    <div
      data-testid="fibonacci-overlay"
      aria-hidden
      // z-0: beneath the areas of interest and the position box. It is
      // context, not a level anything is placed at.
      className="pointer-events-none absolute inset-0 z-0"
    >
      <div
        // Opacity learned the hard way twice on this page: a 4% fill under
        // the position box's 15% green band is invisible, and an overlay
        // nobody can see is indistinguishable from one that failed to render.
        className="absolute left-0 right-0 border-y border-dashed border-accent/50
                   bg-accent/[0.09]"
        style={{ top, height }}
      />
      {fibs.map((f) => (
        <div key={f.ratio} className="absolute left-0 right-0" style={{ top: f.y }}>
          <div className="h-px w-full bg-accent/35" />
          {labels.has(f.ratio) && (
            <span className="num absolute left-1 -translate-y-1/2 rounded
                             bg-surface-0/90 px-1 text-[9px] text-accent
                             ring-1 ring-accent/20">
              {(f.ratio * 100).toFixed(1)}% · {formatPrice(f.price)}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}
