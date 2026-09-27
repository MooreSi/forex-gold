import { describe, expect, it } from "vitest";
import {
  MAX_SOURCES, gatePoint, pathFor, pickSources, pointAt, sentence, sourcePoint,
  stopsByGate, type BrainEvent, type BrainGate,
} from "../content/brainLayout";

const GATES: BrainGate[] = ["auto", "halt", "slots", "broker"].map((key) => ({
  key, label: key.toUpperCase(), blocking: false, detail: "",
}));

function ev(over: Partial<BrainEvent> = {}): BrainEvent {
  return { key: "tg:1", ts: 1, kind: "telegram", source: "GOLD X", direction: "BUY",
           outcome: "blocked", gate: "halt", reason: "paused", ...over };
}

describe("pathFor", () => {
  it("runs from the source through every gate up to the deciding one", () => {
    const pts = pathFor(ev({ gate: "slots" }), GATES, ["GOLD X"])!;
    expect(pts).toHaveLength(1 + 3);
    expect(pts[0]).toEqual(sourcePoint(0, 1));
    expect(pts[3]).toEqual(gatePoint(2, GATES.length));
  });

  it("an executed signal reaches the broker", () => {
    const pts = pathFor(ev({ gate: "broker", outcome: "executed" }), GATES, ["GOLD X"])!;
    expect(pts.at(-1)).toEqual(gatePoint(3, GATES.length));
  });

  it("an unknown gate or source has no path rather than a guessed one", () => {
    expect(pathFor(ev({ gate: "nope" }), GATES, ["GOLD X"])).toBeNull();
    expect(pathFor(ev(), GATES, ["SOMEONE ELSE"])).toBeNull();
  });
});

describe("pointAt", () => {
  const pts = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }];
  it("walks the path by distance", () => {
    expect(pointAt(pts, 0)).toEqual({ x: 0, y: 0 });
    expect(pointAt(pts, 0.25)).toEqual({ x: 5, y: 0 });
    expect(pointAt(pts, 0.75)).toEqual({ x: 10, y: 5 });
    expect(pointAt(pts, 1)).toEqual({ x: 10, y: 10 });
  });
  it("clamps outside 0..1", () => {
    expect(pointAt(pts, 2)).toEqual({ x: 10, y: 10 });
  });
});

describe("pickSources", () => {
  it("keeps the most recently active, once each, up to the cap", () => {
    const events = Array.from({ length: 12 }, (_, i) => ev({ key: `k${i}`, ts: i, source: `S${i % 10}` }));
    const got = pickSources(events);
    expect(got).toHaveLength(MAX_SOURCES);
    expect(got[0]).toBe("S1");   // ts 11
    expect(new Set(got).size).toBe(got.length);
  });
});

describe("sentence", () => {
  it("names the gate by its label and carries the reason", () => {
    expect(sentence(ev(), GATES)).toBe("GOLD X BUY: held at HALT: paused");
  });
  it("says an executed signal reached the broker", () => {
    expect(sentence(ev({ outcome: "executed", gate: "broker", reason: "" }), GATES))
      .toBe("GOLD X BUY: passed every gate, sent to the broker");
  });
  it("says queued, not held, for a queued signal", () => {
    expect(sentence(ev({ outcome: "queued", gate: "slots", reason: "" }), GATES))
      .toBe("GOLD X BUY: queued at SLOTS");
  });
});

describe("stopsByGate", () => {
  it("counts only the decisions a gate stopped", () => {
    expect(stopsByGate([ev(), ev({ key: "b" }), ev({ key: "c", outcome: "executed", gate: "broker" })]))
      .toEqual({ halt: 2 });
  });
});
