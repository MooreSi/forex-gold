import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * Two panes side by side with a divider the operator can drag.
 *
 * Asked for on 2026-09-21: "reduce the width of the chart and increase the
 * width of the open positions table so the detail within the open positions
 * can be viewed easily, may be easier to make the tables adjustable on the
 * screen". Both halves of that are here — a wider default for the right-hand
 * pane, and a divider, because how much chart against how much table is a
 * judgement that changes with what is being looked at and with the size of the
 * screen it is being looked at on.
 *
 * **The split is remembered per `storageKey`, in this browser only.** It is a
 * view preference, not a setting: it decides nothing about trading, it is
 * different on a laptop and on the desk monitor, and putting it in the shared
 * `config.yaml` would make one machine's window shape the other machine's
 * problem. `localStorage` can throw — private windows, blocked site data — so
 * every read and write is guarded and the default is what a failure falls back
 * to.
 *
 * **Keyboard as well as pointer.** The divider is a real `separator` with
 * arrow keys, because a layout only a mouse can change is one that some people
 * cannot change.
 *
 * Below `md` the panes stack and the divider is not rendered: dragging a
 * vertical split on a phone-width screen is not a thing worth having.
 *
 * **Each slot is a flex column, not a plain block.** A panel in a block slot
 * is a block box at content height, and `height: 100%` on anything inside it
 * resolves against an auto height — that is, against nothing. On 2026-09-22
 * that cost the Chart tab its candles: the canvas came back 30 pixels tall,
 * the height of the only piece of the chart that can size itself, with a
 * correct 200-candle payload behind it. A slot that is a flex column gives the
 * panel a height flex layout has resolved, and percentages below it resolve
 * too. A panel that wants the room still has to ask, with `flex-1`.
 */
interface SplitPaneProps {
  /** Where this pane's position is remembered. Unique per screen. */
  storageKey: string;
  /** Right-hand pane's share of the width, as a percentage. */
  defaultRightPct: number;
  minRightPct?: number;
  maxRightPct?: number;
  left: ReactNode;
  right: ReactNode;
  className?: string;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

function remembered(key: string, fallback: number): number {
  try {
    const raw = localStorage.getItem(key);
    const n = raw === null ? NaN : Number(raw);
    return Number.isFinite(n) ? n : fallback;
  } catch {
    return fallback;
  }
}

export function SplitPane({
  storageKey, defaultRightPct, minRightPct = 15, maxRightPct = 70,
  left, right, className,
}: SplitPaneProps) {
  const [rightPct, setRightPct] = useState(() =>
    clamp(remembered(storageKey, defaultRightPct), minRightPct, maxRightPct));
  const [dragging, setDragging] = useState(false);
  const host = useRef<HTMLDivElement>(null);

  const put = useCallback((next: number) => {
    const value = clamp(next, minRightPct, maxRightPct);
    setRightPct(value);
    try {
      localStorage.setItem(storageKey, String(value));
    } catch {
      // A remembered width is a convenience. Losing it is not worth an error.
    }
  }, [storageKey, minRightPct, maxRightPct]);

  useEffect(() => {
    if (!dragging) return;
    const move = (e: MouseEvent) => {
      const box = host.current?.getBoundingClientRect();
      if (!box || box.width === 0) return;
      put(((box.right - e.clientX) / box.width) * 100);
    };
    const stop = () => setDragging(false);
    // Mouse events, not pointer events. The divider is only rendered at `md`
    // and above, where there is a mouse; and jsdom's pointer events carry no
    // `clientX` at all, so a pointer-based drag is one that cannot be tested.
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", stop);
    window.addEventListener("blur", stop);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", stop);
      window.removeEventListener("blur", stop);
    };
  }, [dragging, put]);

  return (
    <div
      ref={host}
      className={cn("flex h-full min-h-0 flex-col gap-3 md:flex-row md:gap-0", className)}
      // While dragging, the pointer is often over the chart canvas rather than
      // the divider. Without this the browser starts selecting text instead.
      style={dragging ? { userSelect: "none", cursor: "col-resize" } : undefined}
    >
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">{left}</div>

      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the panes"
        aria-valuenow={Math.round(rightPct)}
        aria-valuemin={minRightPct}
        aria-valuemax={maxRightPct}
        tabIndex={0}
        data-testid={`split-${storageKey}`}
        onMouseDown={(e) => { e.preventDefault(); setDragging(true); }}
        onDoubleClick={() => put(defaultRightPct)}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") { e.preventDefault(); put(rightPct + 2); }
          if (e.key === "ArrowRight") { e.preventDefault(); put(rightPct - 2); }
          if (e.key === "Home") { e.preventDefault(); put(defaultRightPct); }
        }}
        className={cn(
          "hidden shrink-0 cursor-col-resize md:block",
          // A 3px hit target is a divider only a steady hand can grab. The
          // visible line is the 1px child; the padding around it is the grip.
          "group relative w-3",
        )}
      >
        <span
          aria-hidden
          className={cn(
            "absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-colors",
            dragging ? "bg-accent" : "bg-line group-hover:bg-accent",
          )}
        />
      </div>

      <div
        className="flex min-h-0 flex-col md:min-w-0"
        style={{ flexBasis: `${rightPct}%`, flexGrow: 0, flexShrink: 0 }}
      >
        {right}
      </div>
    </div>
  );
}
