import { useCallback, useId, useRef, useState, type ReactElement, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { HelpCircle } from "lucide-react";

/**
 * Hover help, on one control.
 *
 * Asked for on 2026-09-21: "add tooltips to all of the selectable, adjustable
 * fields/items when you hover over them". Half of this dashboard already
 * carried a `title` attribute, which the browser shows after about a second in
 * a style that does not follow the theme; the other half carried nothing at
 * all. This is the one place hover help is drawn, so those two stop drifting.
 *
 * **Hand-rolled rather than `@radix-ui/react-tooltip`**, which is already a
 * dependency and was the obvious choice. Its content is positioned by
 * floating-ui, and under jsdom -- where every element measures 0x0 -- that
 * settles into a render loop that never stops: the tooltip DOES open, and then
 * every `findBy*` in the test times out because React never goes idle. A
 * component this small is not worth a class of test that cannot be written.
 * The real dependency is untouched; nothing else in the app uses it yet.
 *
 * **The wrapper is `display: contents`.** It generates no box, so putting this
 * around an input or a table cell changes no layout anywhere -- which is what
 * made it safe to apply across every panel in one pass. The bubble itself is
 * portalled to `document.body` and positioned against the trigger's own
 * rectangle, so it is not clipped by the `overflow-hidden` on the header or by
 * a panel's scroll container.
 *
 * **A missing label renders the control untouched.** A bubble that opens on an
 * empty box covers the thing it points at and says nothing, so callers may
 * pass a hint that is sometimes undefined without guarding it.
 */
interface TooltipProps {
  /** What to say. Nothing renders when this is empty. */
  label?: ReactNode;
  side?: "top" | "bottom";
  children: ReactElement | ReactNode;
}

interface At {
  left: number;
  top: number;
  side: "top" | "bottom";
}

/** Where the bubble goes: above the control, or below it when there is no room. */
function place(rect: DOMRect, side: "top" | "bottom"): At {
  const wantsAbove = side === "top" && rect.top > 80;
  return {
    left: rect.left + rect.width / 2,
    top: wantsAbove ? rect.top - 8 : rect.bottom + 8,
    side: wantsAbove ? "top" : "bottom",
  };
}

export function Tooltip({ label, side = "top", children }: TooltipProps) {
  const [at, setAt] = useState<At | null>(null);
  const host = useRef<HTMLSpanElement>(null);
  const id = useId();

  const open = useCallback(() => {
    // The first element child, not the wrapper: the wrapper has no box of its
    // own, so its rectangle is empty and the bubble would land at 0,0.
    const target = host.current?.firstElementChild ?? host.current;
    if (!target) return;
    setAt(place(target.getBoundingClientRect(), side));
  }, [side]);

  const close = useCallback(() => setAt(null), []);

  if (label == null || label === "") return <>{children}</>;

  return (
    <span
      ref={host}
      className="contents"
      onMouseEnter={open}
      onMouseLeave={close}
      onFocusCapture={open}
      onBlurCapture={close}
      aria-describedby={at ? id : undefined}
    >
      {children}
      {at
        && createPortal(
          <span
            id={id}
            role="tooltip"
            style={{
              left: at.left,
              top: at.top,
              transform: `translate(-50%, ${at.side === "top" ? "-100%" : "0"})`,
            }}
            className={
              "pointer-events-none fixed z-[80] max-w-xs rounded border border-line "
              + "bg-surface-3 px-2 py-1.5 text-[11px] leading-snug text-ink-1 shadow-lg"
            }
          >
            {label}
          </span>,
          document.body,
        )}
    </span>
  );
}

/**
 * A label with a help mark beside it.
 *
 * For a field whose explanation is too long to sit under it as a hint, and for
 * the many that had no explanation on screen at all. The mark is focusable so
 * the help is reachable without a mouse — a tooltip only a pointer can open is
 * one that does not exist for anybody using the keyboard.
 */
export function LabelWithHelp({ label, help }: { label: ReactNode; help?: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1">
      {label}
      {help ? (
        <Tooltip label={help}>
          <button
            type="button"
            aria-label={typeof label === "string" ? `What is ${label}?` : "What is this?"}
            // A help mark must not submit the form it decorates.
            onClick={(e) => e.preventDefault()}
            className="text-ink-3 transition-colors hover:text-ink-1"
          >
            <HelpCircle size={11} aria-hidden />
          </button>
        </Tooltip>
      ) : null}
    </span>
  );
}
