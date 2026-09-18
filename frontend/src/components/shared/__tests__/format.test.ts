import { describe, expect, it } from "vitest";
import {
  formatBrokerTime, formatLots, formatMoney, formatPercent,
  formatPrice, formatSignedMoney, pnlColour,
} from "../format";

describe("money", () => {
  it("puts the sign outside the currency symbol", () => {
    expect(formatMoney(-12.5)).toBe("-$12.50");
  });

  it("gives a P&L number an explicit + so it cannot be read as a plain number", () => {
    expect(formatSignedMoney(12.5)).toBe("+$12.50");
    expect(formatSignedMoney(-12.5)).toBe("-$12.50");
  });

  it("renders absent data as an em dash rather than as zero", () => {
    // A zero is a number the user will act on. Missing data is not.
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney(undefined)).toBe("—");
    expect(formatMoney(Number.NaN)).toBe("—");
    expect(formatPrice(null)).toBe("—");
    expect(formatLots(null)).toBe("—");
    expect(formatPercent(null)).toBe("—");
  });
});

describe("broker timestamps", () => {
  it("shifts the MT5 stamp back out of UTC+3 before reading it", () => {
    // MT5 encodes its server time (UTC+3) as if it were a UTC epoch. 12:00
    // UTC+3 is 09:00 UTC, which is 10:00 in London on this summer date.
    const noonServerTime = Date.UTC(2026, 5, 15, 12, 0, 0) / 1000;
    expect(formatBrokerTime(noonServerTime)).toContain("10:00");
  });

  it("does not shift it the wrong way", () => {
    // Negative control: adding the offset instead of subtracting it gives
    // 16:00. If this ever passes, every trade time is six hours out.
    const noonServerTime = Date.UTC(2026, 5, 15, 12, 0, 0) / 1000;
    expect(formatBrokerTime(noonServerTime)).not.toContain("16:00");
  });

  it("renders a missing timestamp as absent, not as 1970", () => {
    expect(formatBrokerTime(null)).toBe("—");
  });
});

describe("pnlColour", () => {
  it("is green for profit and red for loss", () => {
    expect(pnlColour(1)).toBe("text-profit");
    expect(pnlColour(-1)).toBe("text-loss");
  });

  it("is neutral for flat and for absent, so zero does not read as a win", () => {
    expect(pnlColour(0)).toBe("text-ink-2");
    expect(pnlColour(null)).toBe("text-ink-2");
  });
});
