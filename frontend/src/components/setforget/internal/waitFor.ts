/**
 * How long a resting order is likely to wait, in words.
 *
 * Shared by the setup card and the confirmation dialog so the two cannot
 * describe the same wait differently — which is the shape of the bug this
 * exists because of: on 2026-09-21 an order went out at a zone days away and
 * nothing on either screen said so.
 *
 * The distance and the day count are both measured by the backend. This only
 * chooses the wording.
 */
export function waitFor(days: number | null): string {
  if (days === null) return "wait unknown — the daily range could not be read";
  if (days < 0.5) return "price is there now";
  if (days < 1.5) return "about a day away";
  return `about ${Math.round(days)} days away`;
}

/** True when the wait is long enough that it deserves saying loudly. */
export function isALongWait(days: number | null): boolean {
  return days !== null && days >= 1.5;
}
