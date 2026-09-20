import { describe, expect, it } from "vitest";
import {
  MACHINE_CLOCK, formatOffset, offsetChoices, offsetToSave, selectedOffset,
} from "../internal/clockOffsets";

describe("formatOffset", () => {
  it("formats a whole-hour offset", () => {
    expect(formatOffset(60)).toBe("UTC+01:00");
  });

  it("formats UTC itself with a plus", () => {
    expect(formatOffset(0)).toBe("UTC+00:00");
  });

  it("keeps the sign on the whole offset, not just the hours", () => {
    // -210 is three and a half hours behind UTC. Formatting the parts
    // independently gives "UTC-02:30" — an hour out and plausible-looking.
    expect(formatOffset(-210)).toBe("UTC-03:30");
  });

  it("formats a negative whole hour", () => {
    expect(formatOffset(-300)).toBe("UTC-05:00");
  });

  it("formats the three-quarter-hour zones real places use", () => {
    expect(formatOffset(345)).toBe("UTC+05:45");
    expect(formatOffset(765)).toBe("UTC+12:45");
  });
});

describe("offsetChoices", () => {
  it("offers the machine clock first", () => {
    expect(offsetChoices()[0].value).toBe(MACHINE_CLOCK);
  });

  it("covers every zone in use, from UTC-12:00 to UTC+14:00", () => {
    const labels = offsetChoices().map((c) => c.label);
    expect(labels).toContain("UTC-12:00");
    expect(labels).toContain("UTC+14:00");
  });

  it("steps in quarter hours so half- and quarter-hour zones exist", () => {
    const labels = offsetChoices().map((c) => c.label);
    expect(labels).toContain("UTC+05:30");
    expect(labels).toContain("UTC+05:45");
  });
});

describe("selectedOffset", () => {
  it("starts on the machine clock when nothing is configured", () => {
    expect(selectedOffset(null)).toBe(MACHINE_CLOCK);
    expect(selectedOffset(undefined)).toBe(MACHINE_CLOCK);
  });

  it("does not mistake UTC for the machine clock", () => {
    // Zero is falsy. `||` here silently moves a UTC+0 user onto the
    // machine's clock, which on a VPS is the bug this control exists to fix.
    expect(selectedOffset(0)).toBe("0");
  });

  it("starts on the configured offset", () => {
    expect(selectedOffset(330)).toBe("330");
  });
});

describe("offsetToSave", () => {
  it("sends null for the machine clock", () => {
    expect(offsetToSave(MACHINE_CLOCK)).toBeNull();
  });

  it("sends zero as a number, not as null", () => {
    expect(offsetToSave("0")).toBe(0);
  });

  it("sends a chosen offset as a number", () => {
    expect(offsetToSave("-210")).toBe(-210);
  });
});
