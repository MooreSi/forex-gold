import { useEffect, useMemo, useRef, useState } from "react";
import {
  SPINE_Y, VIEW, gatePoint, pathFor, pathLength, pointAt, sourcePoint, stopsByGate,
  type BrainEvent, type BrainGate, type Point,
} from "../content/brainLayout";

interface Pulse {
  key: string;
  pts: Point[];
  start: number;
  dur: number;
  outcome: BrainEvent["outcome"];
  gate: string;
}

const COLOUR = {
  executed: "var(--color-profit)",
  queued: "var(--color-warning)",
  blocked: "var(--color-loss)",
};
const REPLAY_ON_OPEN = 6;
const STAGGER_MS = 350;
const BURST_MS = 900;

function reducedMotion(): boolean {
  return typeof window !== "undefined"
    && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
}

// A fixed field of faint "neurons" behind everything: decoration only, seeded
// so it does not jump between renders.
const FIELD = Array.from({ length: 70 }, (_, i) => ({
  x: (Math.sin(i * 12.9898) * 43758.5453 % 1 + 1) % 1 * VIEW.w,
  y: (Math.sin(i * 78.233) * 12345.678 % 1 + 1) % 1 * VIEW.h,
}));

function gateFill(g: BrainGate): string {
  if (g.key === "broker") return "var(--color-profit)";
  if (g.blocking === true) return "var(--color-loss)";
  if (g.blocking === null) return "var(--color-ink-3)";
  return "var(--color-accent)";
}

/**
 * The picture: sources on the left, the gates as a spine, the broker at the
 * end. Each new decision is a pulse that travels to the gate that decided it
 * and bursts there -- green at the broker, amber if queued, red if held.
 * A gate holding orders right now glows red.
 */
export function BrainGraph({ gates, events, sources }: {
  gates: BrainGate[]; events: BrainEvent[]; sources: string[];
}) {
  const seen = useRef<Set<string> | null>(null);
  const pulses = useRef<Pulse[]>([]);
  const [now, setNow] = useState(() => performance.now());
  const still = useMemo(reducedMotion, []);

  useEffect(() => {
    const fresh = [...events].sort((a, b) => a.ts - b.ts);
    let queue: BrainEvent[];
    if (seen.current === null) {
      seen.current = new Set(events.map((e) => e.key));
      queue = fresh.slice(-REPLAY_ON_OPEN);
    } else {
      queue = fresh.filter((e) => !seen.current!.has(e.key));
      queue.forEach((e) => seen.current!.add(e.key));
    }
    const t0 = performance.now();
    queue.forEach((e, i) => {
      const pts = pathFor(e, gates, sources);
      if (!pts) return;
      pulses.current.push({
        key: e.key, pts, outcome: e.outcome, gate: e.gate,
        start: t0 + i * STAGGER_MS, dur: still ? 1 : 500 + pathLength(pts) * 1.6,
      });
    });
  }, [events, gates, sources, still]);

  useEffect(() => {
    let frame = 0;
    const tick = () => {
      const t = performance.now();
      pulses.current = pulses.current.filter((p) => t < p.start + p.dur + BURST_MS);
      setNow(t);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, []);

  const stops = stopsByGate(events);
  const spine = gates.map((_, i) => gatePoint(i, gates.length));
  const first = spine[0] ?? { x: 0, y: SPINE_Y };

  return (
    <svg viewBox={`0 0 ${VIEW.w} ${VIEW.h}`} className="h-full w-full" role="img"
      aria-label="Live map of entry decisions through the gates">
      <defs>
        <filter id="brain-glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="4" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>

      {FIELD.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r={1.2} fill="var(--color-accent)" opacity={0.12} />
      ))}

      {sources.map((s, i) => {
        const p = sourcePoint(i, sources.length);
        return (
          <path key={`e-${s}`} fill="none" stroke="var(--color-accent)" strokeOpacity={0.18}
            d={`M${p.x},${p.y} Q${(p.x + first.x) / 2},${p.y} ${first.x},${first.y}`} />
        );
      })}
      <polyline points={spine.map((p) => `${p.x},${p.y}`).join(" ")} fill="none"
        stroke="var(--color-accent)" strokeOpacity={0.35} strokeWidth={2} />

      {sources.map((s, i) => {
        const p = sourcePoint(i, sources.length);
        return (
          <g key={`s-${s}`}>
            <circle cx={p.x} cy={p.y} r={7} fill="var(--color-accent)" opacity={0.8} />
            <text x={p.x - 12} y={p.y + 4} textAnchor="end" fontSize={11}
              fill="currentColor" className="text-ink-2">{s.length > 18 ? `${s.slice(0, 17)}…` : s}</text>
          </g>
        );
      })}

      {gates.map((g, i) => {
        const p = spine[i];
        const below = i % 2 === 0;
        const hot = pulses.current.some((q) => q.gate === g.key
          && now > q.start + q.dur && now < q.start + q.dur + BURST_MS);
        return (
          <g key={g.key} data-testid={`brain-gate-${g.key}`} data-blocking={String(g.blocking)}>
            <title>{g.detail ? `${g.label}: ${g.detail}` : g.label}</title>
            {g.blocking === true && (
              <circle cx={p.x} cy={p.y} r={26} fill="var(--color-loss)" opacity={0.18}>
                {!still && <animate attributeName="r" values="20;30;20" dur="1.6s" repeatCount="indefinite" />}
              </circle>
            )}
            <circle cx={p.x} cy={p.y} r={hot ? 17 : 13} fill={gateFill(g)}
              opacity={g.blocking === null ? 0.5 : 0.9} filter="url(#brain-glow)" />
            <text x={p.x} y={below ? p.y + 34 : p.y - 26} textAnchor="middle" fontSize={10.5}
              fill="currentColor" className="text-ink-2">{g.label}</text>
            {stops[g.key] ? (
              <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize={10} fontWeight={700}
                fill="var(--color-surface-0)">{stops[g.key]}</text>
            ) : null}
          </g>
        );
      })}

      {pulses.current.map((q) => {
        const t = (now - q.start) / q.dur;
        if (t < 0) return null;
        const colour = COLOUR[q.outcome];
        if (t >= 1) {
          const end = q.pts[q.pts.length - 1];
          const f = (now - q.start - q.dur) / BURST_MS;
          return <circle key={q.key} cx={end.x} cy={end.y} r={14 + f * 26} fill="none"
            stroke={colour} strokeWidth={2} opacity={1 - f} />;
        }
        return (
          <g key={q.key} filter="url(#brain-glow)">
            {[0.06, 0.04, 0.02].map((lag, i) => {
              const p = pointAt(q.pts, t - lag);
              return <circle key={i} cx={p.x} cy={p.y} r={3 + i} fill={colour} opacity={0.15 + i * 0.15} />;
            })}
            <circle cx={pointAt(q.pts, t).x} cy={pointAt(q.pts, t).y} r={6} fill={colour} />
          </g>
        );
      })}
    </svg>
  );
}
