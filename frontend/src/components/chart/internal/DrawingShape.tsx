import type { MouseEvent as ReactMouseEvent, ReactElement } from "react";
import { formatPrice } from "@/components/shared/format";
import { fibPrices, type Drawing, type DrawingPoint } from "./drawingGeometry";
import { PositionShape } from "./PositionShape";

export type Project = (p: DrawingPoint) => { x: number; y: number } | null;

interface DrawingShapeProps {
  drawing: Drawing;
  project: Project;
  /** Price to pixel, for the Fibonacci levels between the two anchors. */
  priceToY: (price: number) => number | null;
  width: number;
  colour: string;
  selected: boolean;
  onBodyDown: (e: ReactMouseEvent, d: Drawing) => void;
  onHandleDown: (e: ReactMouseEvent, d: Drawing, index: number) => void;
}

/** Wide and invisible: a one-pixel line is too thin to click. */
const HIT = { stroke: "transparent", strokeWidth: 10, pointerEvents: "stroke" as const };

/**
 * One stored drawing, projected to pixels. The anchors are (time, price);
 * `project` turns them into positions for the chart's current zoom, so this
 * is called again on every pan and zoom.
 */
export function DrawingShape({
  drawing, project, priceToY, width, colour, selected, onBodyDown, onHandleDown,
}: DrawingShapeProps) {
  const pts = drawing.points.map(project);
  if (pts.some((p) => p === null)) return null;
  const xy = pts as { x: number; y: number }[];
  const [a, b] = xy;
  const stroke = { stroke: colour, strokeWidth: selected ? 2 : 1.5 };
  const id = `drawing-${drawing.kind}-${drawing.id}`;
  const down = (e: ReactMouseEvent) => onBodyDown(e, drawing);

  let body: ReactElement;
  if (drawing.kind === "position") {
    body = <PositionShape drawing={drawing} xy={xy} colour={colour} onDown={down} />;
  } else if (drawing.kind === "hline") {
    body = (
      <g onMouseDown={down} style={{ cursor: "move" }}>
        <line x1={0} x2={width} y1={a.y} y2={a.y} {...HIT} />
        <line data-testid={id} x1={0} x2={width} y1={a.y} y2={a.y} {...stroke}
          pointerEvents="stroke" />
        <text x={width - 6} y={a.y - 4} textAnchor="end" fontSize={10} fill={colour}>
          {formatPrice(drawing.points[0].price)}
        </text>
      </g>
    );
  } else if (drawing.kind === "rect") {
    const x = Math.min(a.x, b.x);
    const y = Math.min(a.y, b.y);
    body = (
      <rect data-testid={id} onMouseDown={down} style={{ cursor: "move" }}
        x={x} y={y} width={Math.abs(b.x - a.x)} height={Math.abs(b.y - a.y)}
        fill={colour} fillOpacity={0.12} {...stroke} pointerEvents="all" />
    );
  } else if (drawing.kind === "fib") {
    const x1 = Math.min(a.x, b.x);
    const x2 = Math.max(a.x, b.x);
    const levels = fibPrices(drawing.points[0].price, drawing.points[1].price);
    body = (
      <g data-testid={id} onMouseDown={down} style={{ cursor: "move" }}>
        <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={colour} strokeDasharray="3 3"
          strokeWidth={1} />
        {levels.map(({ level, price }) => {
          const y = priceToY(price);
          if (y === null) return null;
          return (
            <g key={level}>
              <line x1={x1} x2={x2} y1={y} y2={y} {...HIT} />
              <line data-level={level} x1={x1} x2={x2} y1={y} y2={y} {...stroke}
                pointerEvents="stroke" />
              <text x={x1 + 4} y={y - 3} fontSize={10} fill={colour}>
                {`${(level * 100).toFixed(1)}%  ${formatPrice(price)}`}
              </text>
            </g>
          );
        })}
      </g>
    );
  } else {
    body = (
      <g onMouseDown={down} style={{ cursor: "move" }}>
        <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} {...HIT} />
        <line data-testid={id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} {...stroke}
          pointerEvents="stroke" />
      </g>
    );
  }

  return (
    <g>
      {body}
      {selected && xy.map((p, i) => (
        <circle
          key={i}
          data-testid={`drawing-handle-${drawing.id}-${i}`}
          cx={p.x} cy={p.y} r={5}
          fill="var(--color-surface-1, #fff)" stroke={colour} strokeWidth={1.5}
          style={{ cursor: "grab" }}
          pointerEvents="all"
          onMouseDown={(e) => onHandleDown(e, drawing, i)}
        />
      ))}
    </g>
  );
}
