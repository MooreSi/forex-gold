/**
 * Where everything sits in the brain picture, and what each decision says.
 * Pure functions, so the geometry and the wording are testable without a
 * browser (docs/todo/008).
 *
 * Sources on the left, the gates as a spine in the order a signal meets them,
 * the broker at the end. A pulse travels the spine and stops at the gate that
 * decided it -- the one the backend named, never a guess made here.
 */

export interface BrainGate {
  key: string;
  label: string;
  blocking: boolean | null;
  detail: string;
}

export interface BrainEvent {
  key: string;
  ts: number;
  kind: string;
  source: string;
  direction: string;
  outcome: "executed" | "queued" | "blocked";
  gate: string;
  reason: string;
}

export interface Point { x: number; y: number }

export const VIEW = { w: 1000, h: 440 };
export const SPINE_Y = 250;
const SPINE_X0 = 300;
const SPINE_X1 = 960;
const SOURCE_X = 170;
export const MAX_SOURCES = 8;

export function gatePoint(index: number, count: number): Point {
  const step = count > 1 ? (SPINE_X1 - SPINE_X0) / (count - 1) : 0;
  // A gentle wave, so the spine reads as a nerve rather than a ruler.
  return { x: SPINE_X0 + index * step, y: SPINE_Y + Math.sin(index * 0.9) * 18 };
}

/** Distinct sources, most recently active first, capped at MAX_SOURCES. */
export function pickSources(events: BrainEvent[]): string[] {
  const out: string[] = [];
  for (const e of [...events].sort((a, b) => b.ts - a.ts)) {
    if (!out.includes(e.source)) out.push(e.source);
    if (out.length === MAX_SOURCES) break;
  }
  return out;
}

export function sourcePoint(index: number, count: number): Point {
  const top = 50;
  const bottom = VIEW.h - 50;
  const step = count > 1 ? (bottom - top) / (count - 1) : 0;
  return { x: SOURCE_X, y: count > 1 ? top + index * step : SPINE_Y };
}

/**
 * The path a pulse takes: its source, then every gate up to and including the
 * one that decided it. An unknown gate or source has no path (null) rather
 * than a made-up one.
 */
export function pathFor(e: BrainEvent, gates: BrainGate[], sources: string[]): Point[] | null {
  const s = sources.indexOf(e.source);
  const g = gates.findIndex((x) => x.key === e.gate);
  if (s < 0 || g < 0) return null;
  const pts = [sourcePoint(s, sources.length)];
  for (let i = 0; i <= g; i += 1) pts.push(gatePoint(i, gates.length));
  return pts;
}

export function pathLength(pts: Point[]): number {
  let total = 0;
  for (let i = 1; i < pts.length; i += 1) {
    total += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
  }
  return total;
}

/** The point `t` (0..1) of the way along a path. */
export function pointAt(pts: Point[], t: number): Point {
  const target = Math.max(0, Math.min(1, t)) * pathLength(pts);
  let walked = 0;
  for (let i = 1; i < pts.length; i += 1) {
    const seg = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    if (walked + seg >= target && seg > 0) {
      const f = (target - walked) / seg;
      return { x: pts[i - 1].x + (pts[i].x - pts[i - 1].x) * f,
               y: pts[i - 1].y + (pts[i].y - pts[i - 1].y) * f };
    }
    walked += seg;
  }
  return pts[pts.length - 1];
}

/** One line for the thought stream. */
export function sentence(e: BrainEvent, gates: BrainGate[]): string {
  const label = gates.find((g) => g.key === e.gate)?.label ?? e.gate;
  const who = `${e.source}${e.direction ? ` ${e.direction}` : ""}`;
  if (e.outcome === "executed") return `${who}: passed every gate, sent to the broker`;
  const why = e.reason ? `: ${e.reason}` : "";
  if (e.outcome === "queued") return `${who}: queued at ${label}${why}`;
  return `${who}: held at ${label}${why}`;
}

/** How many of the recent decisions each gate stopped. */
export function stopsByGate(events: BrainEvent[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const e of events) {
    if (e.outcome !== "executed") out[e.gate] = (out[e.gate] ?? 0) + 1;
  }
  return out;
}
