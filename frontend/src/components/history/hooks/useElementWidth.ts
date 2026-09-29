import { useEffect, useRef, useState } from "react";

/**
 * The rendered width of an element, in whole pixels, kept current as it
 * resizes.
 *
 * `null` until the first measurement, and for good where there is no
 * `ResizeObserver` (jsdom): callers draw at a default size then. Rounded, so a
 * sub-pixel wobble during a layout does not redraw a chart.
 */
export function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState<number | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const w = Math.round(el.getBoundingClientRect().width);
      if (w > 0) setWidth(w);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return [ref, width] as const;
}
