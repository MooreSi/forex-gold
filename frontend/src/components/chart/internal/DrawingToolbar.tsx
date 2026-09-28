import { AlignJustify, ArrowDownUp, Minus, MousePointer2, Slash, Square, Trash2 } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import type { ChartDrawings, DrawingTool } from "../hooks/useChartDrawings";

const TOOLS: { tool: DrawingTool; label: string; help: string; Icon: LucideIcon }[] = [
  { tool: null, label: "Pointer", help: "Pan and zoom the chart; click a drawing to select it.", Icon: MousePointer2 },
  { tool: "trend", label: "Trend line", help: "Click the start, then the end.", Icon: Slash },
  { tool: "hline", label: "Horizontal level", help: "Click once at the price.", Icon: Minus },
  { tool: "rect", label: "Rectangle", help: "Click one corner, then the opposite corner.", Icon: Square },
  { tool: "fib", label: "Fibonacci retracement", help: "Click the start of the swing, then its end.", Icon: AlignJustify },
  {
    tool: "position", label: "Position",
    help: "Click the entry, then the stop, then the target. A stop under the entry is a long. Select it to turn it into an order.",
    Icon: ArrowDownUp,
  },
];

/**
 * The drawing tools, down the chart's left edge where TradingView keeps
 * them. Drawings are saved as soon as they are placed (docs/todo/011).
 */
export function DrawingToolbar({ d }: { d: ChartDrawings }) {
  const button = "flex h-7 w-7 items-center justify-center rounded transition-colors";
  return (
    <div className="absolute left-1 top-1 z-30 flex flex-col gap-0.5 rounded border border-line bg-surface-1/90 p-0.5">
      {TOOLS.map(({ tool, label, help, Icon }) => (
        <Tooltip key={label} label={`${label}. ${help}`} side="bottom">
          <button
            aria-label={label}
            aria-pressed={d.tool === tool}
            onClick={() => d.setTool(tool)}
            className={cn(button, d.tool === tool
              ? "bg-surface-3 text-ink-1"
              : "text-ink-3 hover:bg-surface-2 hover:text-ink-2")}
          >
            <Icon size={14} />
          </button>
        </Tooltip>
      ))}
      <Tooltip label="Delete the selected drawing (or press Delete)." side="bottom">
        <button
          aria-label="Delete drawing"
          disabled={d.selectedId === null}
          onClick={() => d.selectedId !== null && void d.remove(d.selectedId)}
          className={cn(button, "text-ink-3 hover:bg-surface-2 hover:text-loss disabled:opacity-30")}
        >
          <Trash2 size={14} />
        </button>
      </Tooltip>
      {d.error && (
        <p role="alert" className="absolute left-9 top-0 w-56 rounded bg-surface-2 px-2 py-1 text-[11px] text-loss">
          {d.error}
        </p>
      )}
    </div>
  );
}
