import type { MouseEvent as ReactMouseEvent } from "react";
import { chartColours } from "@/components/shared/chartTheme";
import { formatPrice } from "@/components/shared/format";
import { positionLevels, type Drawing } from "./drawingGeometry";

/** Narrow enough to stay out of the way, wide enough to grab. */
const MIN_WIDTH = 60;

/**
 * A long/short position: the loss zone from entry to stop, the profit zone
 * from entry to target, and a label saying which way it trades (2026-09-28).
 * `xy` is the drawing's points already in pixels; a preview mid-placement
 * has only two of them, and draws the stop zone alone.
 */
export function PositionShape({
  drawing, xy, colour, onDown,
}: {
  drawing: Drawing;
  xy: { x: number; y: number }[];
  colour: string;
  onDown: (e: ReactMouseEvent) => void;
}) {
  const c = chartColours();
  const left = Math.min(...xy.map((p) => p.x));
  const width = Math.max(MIN_WIDTH, Math.max(...xy.map((p) => p.x)) - left);
  const [entry, stop, target] = xy;
  const levels = positionLevels(drawing.points);
  const zone = (to: { y: number }, fill: string, name: string) => (
    <rect data-zone={name} x={left} width={width}
      y={Math.min(entry.y, to.y)} height={Math.abs(to.y - entry.y)}
      fill={fill} fillOpacity={0.18} stroke={fill} strokeOpacity={0.5} pointerEvents="all" />
  );
  const label = drawing.points.length < 3
    ? null
    : levels
      ? `${levels.direction === "BUY" ? "LONG" : "SHORT"}  R:R ${levels.rr}  ` +
        `stop ${formatPrice(levels.stop)}  target ${formatPrice(levels.target)}`
      : "Not a trade: the stop and target must be on opposite sides of the entry";

  return (
    <g data-testid={`drawing-position-${drawing.id}`} onMouseDown={onDown} style={{ cursor: "move" }}>
      {stop && zone(stop, c.loss, "stop")}
      {target && zone(target, c.profit, "target")}
      <line x1={left} x2={left + width} y1={entry.y} y2={entry.y} stroke={colour} strokeWidth={1.5} />
      {label && (
        <text x={left + 4} y={entry.y - 4} fontSize={10} fill={levels ? colour : c.loss}>
          {label}
        </text>
      )}
    </g>
  );
}
