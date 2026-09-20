/**
 * The Trading Clock dropdown's choices.
 *
 * Every window time on the schedule screen is a wall-clock time, and the gate
 * reads them against this clock. On the machine the user is sitting at that is
 * just the machine's own clock. On a VPS it is not.
 */

/** The dropdown value meaning "follow this machine's own clock". */
export const MACHINE_CLOCK = "machine";

/**
 * "UTC-03:30".
 *
 * The sign belongs to the whole offset, not to the hours. Formatting the parts
 * independently turns -210 into "UTC-02:30" — an hour out, and entirely
 * plausible-looking, which is the dangerous kind of wrong. The NiceGUI page
 * carried this same warning.
 */
export function formatOffset(minutes: number): string {
  const sign = minutes < 0 ? "-" : "+";
  const total = Math.abs(minutes);
  const hh = String(Math.floor(total / 60)).padStart(2, "0");
  const mm = String(total % 60).padStart(2, "0");
  return `UTC${sign}${hh}:${mm}`;
}

export interface OffsetChoice {
  value: string;
  label: string;
}

/**
 * Machine clock first, then every 15 minutes from UTC-12:00 to UTC+14:00.
 *
 * 15-minute steps rather than whole hours: +05:30 (India), +05:45 (Nepal) and
 * +12:45 (Chatham Islands) are real places, and a coarser list leaves everyone
 * in them with no correct entry to pick.
 */
export function offsetChoices(): OffsetChoice[] {
  const out: OffsetChoice[] = [
    { value: MACHINE_CLOCK, label: "This machine's clock (default)" },
  ];
  for (let m = -12 * 60; m <= 14 * 60; m += 15) {
    out.push({ value: String(m), label: formatOffset(m) });
  }
  return out;
}

/**
 * Which entry the dropdown should start on.
 *
 * `configured` is null for "follow the machine" and a number otherwise —
 * **including 0**, which is falsy. Anything using `||` here puts a UTC+0 user
 * back on the machine's clock, which on a VPS is the exact bug this control
 * exists to fix.
 */
export function selectedOffset(configured: number | null | undefined): string {
  return configured === null || configured === undefined
    ? MACHINE_CLOCK
    : String(configured);
}

/** The value to send. `null` means "follow the machine". */
export function offsetToSave(chosen: string): number | null {
  return chosen === MACHINE_CLOCK ? null : Number(chosen);
}
