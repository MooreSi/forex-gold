import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { ladderLevels, ladderRr, maxLadderLevel } from "../internal/ladderRr";

/**
 * The TypeScript half of the ladder R:R parity contract.
 *
 * The arithmetic runs in two languages: `ladder_rr` in
 * backend/src/services/broker/ea_templates.py, and `ladderRr` here, because
 * the EA template editor's readout has to move while the operator is typing
 * and a round trip per keystroke is not a readout, it is a lag.
 *
 * Neither side is the reference. Both are checked against the SAME case file,
 * whose expected numbers were worked out from the rule rather than captured
 * from a run, so a change to one implementation that the other does not follow
 * fails on the side that did not move. The Python half is
 * tests/core/test_ladder_rr_shared_cases.py.
 *
 * The file is read from disk rather than imported: it lives outside
 * frontend/src on purpose, since it belongs to neither side.
 */
interface Case {
  id: string;
  why: string;
  sl_pips: number;
  close_full_on_last: boolean;
  levels: [number, number, number][];
  rows: [number, number, number, number][];
  total_r: number;
  remaining: number;
  pct_sum: number;
}

/** The case file sits at the repo root, above frontend/, so it is found by
 *  walking up rather than by a relative hop that breaks if this file moves. */
function sharedCasesPath(): string {
  const relative = "tests/fixtures/ladder_rr_cases.json";
  let dir = process.cwd();
  for (;;) {
    const candidate = resolve(dir, relative);
    if (existsSync(candidate)) return candidate;
    const up = dirname(dir);
    if (up === dir) throw new Error(`Could not find ${relative} above ${process.cwd()}`);
    dir = up;
  }
}

const CASES_PATH = sharedCasesPath();
const cases: Case[] = JSON.parse(readFileSync(CASES_PATH, "utf-8")).cases;

describe("the shared case file", () => {
  it("is where both sides expect it", () => {
    // A parity file the tests silently stop finding proves nothing on either
    // side. The Python half asserts the same ids.
    expect(cases.map((c) => c.id).sort()).toEqual([
      "basic-three-level", "close-full-banks-the-rest", "no-stop-no-readout",
      "oversubscribed-grid", "runner-left-open", "same-ladder-without-close-full",
      "zero-pip-level-is-skipped",
    ]);
  });
});

describe("TypeScript answers the shared cases", () => {
  it.each(cases.map((c) => [c.id, c] as const))("%s", (_id, c) => {
    const got = ladderRr(c.sl_pips, c.levels, c.close_full_on_last);

    expect(got.rows.map((r) => [
      r.level, round(r.rr), round(r.closed), round(r.contribution),
    ])).toEqual(c.rows.map((r) => [r[0], round(r[1]), round(r[2]), round(r[3])]));
    expect(round(got.totalR)).toBe(round(c.total_r));
    expect(round(got.remaining)).toBe(round(c.remaining));
    expect(round(got.pctSum)).toBe(round(c.pct_sum));
  });
});

function round(n: number): number {
  return Math.round(n * 1e6) / 1e6;
}

describe("reading a ladder out of the editor's draft", () => {
  it("keeps a level that has a % but no distance", () => {
    // It is not a target, so it earns no row — but it is still a configured
    // level, which is what decides where close_full_on_last banks.
    const levels = ladderLevels({ tp1_pips: "0", tp1_pct: "50", tp2_pips: "80",
      tp2_pct: "50" }, "tp", 2);

    expect(levels).toEqual([[1, 0, 50], [2, 80, 50]]);
  });

  it("drops a level that is empty on both rows", () => {
    const levels = ladderLevels({ tp1_pips: "40", tp1_pct: "50", tp2_pips: "0",
      tp2_pct: "0" }, "tp", 2);

    expect(levels).toEqual([[1, 40, 50]]);
  });

  it("reads a half-typed number as nothing rather than as NaN", () => {
    // Numbers are held as strings while editing, so "." and "" both reach
    // here. A NaN would propagate into every R on the row.
    const levels = ladderLevels({ tp1_pips: ".", tp1_pct: "", tp2_pips: "80",
      tp2_pct: "100" }, "tp", 2);

    expect(levels).toEqual([[2, 80, 100]]);
  });

  it("reads the pending ladder off its own prefix", () => {
    const levels = ladderLevels({ tp1_pips: "40", tp1_pct: "100",
      tp_pen1_pips: "25", tp_pen1_pct: "60" }, "tp_pen", 1);

    expect(levels).toEqual([[1, 25, 60]]);
  });
});

describe("how many levels a ladder has", () => {
  it("counts them from the backend's own field list", () => {
    // Not a constant on this side: MAX_TP_LEVELS is the backend's number, and
    // a ladder drawn with a hardcoded count would either hide a level the EA
    // reads or invent one it does not.
    expect(maxLadderLevel(
      ["sl_pips", "tp1_pips", "tp1_pct", "tp2_pips", "tp2_pct", "tp3_pips"], "tp",
    )).toBe(3);
  });

  it("does not count the pending ladder as part of the anchor one", () => {
    expect(maxLadderLevel(["tp1_pips", "tp_pen4_pips"], "tp")).toBe(1);
    expect(maxLadderLevel(["tp1_pips", "tp_pen4_pips"], "tp_pen")).toBe(4);
  });

  it("is zero when the schema describes no ladder at all", () => {
    expect(maxLadderLevel(["sl_pips", "trail_mode"], "tp")).toBe(0);
  });
});
