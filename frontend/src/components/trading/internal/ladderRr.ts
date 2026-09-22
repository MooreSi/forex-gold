/**
 * Reward per unit of risk for a TP ladder, computed as the operator types.
 *
 * **This arithmetic exists twice.** `ladder_rr` in
 * backend/src/services/broker/ea_templates.py is the same rule in Python, and
 * that is deliberate rather than an oversight: the readout under the ladder
 * has to move on every keystroke, and a round trip per character is not a
 * readout, it is a lag.
 *
 * Neither copy is the reference. Both are checked against
 * `tests/fixtures/ladder_rr_cases.json`, whose expected numbers were worked
 * out from the rule rather than captured from a run — so a change to one side
 * that the other does not follow fails on the side that did not move. If you
 * change anything here, change ea_templates.py and the case file with it.
 *
 * Two things the rule is careful about, because both overstate a ladder:
 *
 * **The position is walked as REMAINING lots, not by summing the % column.**
 * That is what the EA does: DoPartialClose takes MathMin(lots, remaining), so
 * a level can never close more than is still open. A 25/25/25/100 ladder sums
 * to 4.00R that way and really banks 2.50R, because TP4's 100% can only take
 * the 25% its own earlier levels left behind.
 *
 * **close_full_on_last means the deepest CONFIGURED level banks whatever is
 * still open**, regardless of its own %. A ladder whose %s stop at 70 is not
 * leaving 30% running unless that switch is off.
 *
 * `totalR` is the all-levels-hit figure and is deliberately NOT
 * probability-weighted: it says what the geometry pays when it works, which
 * is the thing being designed here. What it does not say is how often that
 * happens.
 */

/** One configured level: [level number, pips from entry, % to close there]. */
export type LadderLevel = [number, number, number];

export interface LadderRow {
  level: number;
  /** How far this level reaches, in multiples of the stop. Unaffected by %. */
  rr: number;
  /** What actually closes here once earlier levels have taken their share. */
  closed: number;
  /** What this level contributes to the ladder: rr x closed. */
  contribution: number;
}

export interface LadderRr {
  rows: LadderRow[];
  /** What the ladder returns if price reaches every configured level. */
  totalR: number;
  /** % of the position still open at the end — a runner on the trail/SL. */
  remaining: number;
  /** The raw sum of the % column, before any of it is capped. */
  pctSum: number;
}

/** A draft value, which is a string while the field is being edited. */
function num(raw: unknown): number {
  const n = Number(raw);
  // "" and "." both reach here — numbers are held as strings while editing so
  // that Number("1.") does not eat the decimal point the operator just typed.
  return Number.isFinite(n) ? n : 0;
}

/**
 * The ladder as the draft currently states it.
 *
 * A level with a % but no distance is kept: it is not a target and earns no
 * row, but it is still configured, and that is what decides where
 * close_full_on_last banks the remainder. A level empty on both rows is not
 * configured at all and is dropped.
 */
export function ladderLevels(
  draft: Record<string, unknown>, prefix: string, maxLevel: number,
): LadderLevel[] {
  const levels: LadderLevel[] = [];
  for (let n = 1; n <= maxLevel; n += 1) {
    const pips = num(draft[`${prefix}${n}_pips`]);
    const pct = num(draft[`${prefix}${n}_pct`]);
    if (pips > 0 || pct > 0) levels.push([n, pips, pct]);
  }
  return levels;
}

export function ladderRr(
  slPips: number, levels: LadderLevel[], closeFullOnLast: boolean,
): LadderRr {
  const sl = num(slPips);
  const pctSum = levels.reduce((sum, [, , pct]) => sum + num(pct), 0);
  if (sl <= 0 || levels.length === 0) {
    return { rows: [], totalR: 0, remaining: 100, pctSum };
  }

  const lastLevel = levels[levels.length - 1][0];
  const rows: LadderRow[] = [];
  let remaining = 100;
  let totalR = 0;

  for (const [level, rawPips, rawPct] of levels) {
    const pips = num(rawPips);
    const pct = num(rawPct);
    if (pips <= 0) continue;
    const rr = pips / sl;
    const wanted = closeFullOnLast && level === lastLevel ? remaining : Math.min(pct, remaining);
    const closed = Math.max(0, Math.min(wanted, remaining));
    const contribution = (rr * closed) / 100;
    totalR += contribution;
    remaining = Math.max(0, remaining - closed);
    rows.push({ level, rr, closed, contribution });
  }

  return { rows, totalR, remaining, pctSum };
}

/**
 * How many levels this ladder has, read from the backend's own field list.
 *
 * Not a constant on this side. `MAX_TP_LEVELS` belongs to the backend, and a
 * readout drawn with a hardcoded count would either hide a level the EA reads
 * or invent one it does not.
 */
export function maxLadderLevel(fieldNames: string[], prefix: string): number {
  const pattern = new RegExp(`^${prefix}(\\d+)_pips$`);
  let max = 0;
  for (const name of fieldNames) {
    const found = pattern.exec(name);
    if (found) max = Math.max(max, Number(found[1]));
  }
  return max;
}
