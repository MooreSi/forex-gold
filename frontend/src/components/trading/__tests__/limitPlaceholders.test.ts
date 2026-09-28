/**
 * The limit order form's example prices (owner, 2026-09-28: "fix the limit
 * order placeholder prices"). They were fixed at 2430 / 2432 / 2421 from when
 * gold traded near 2430; at 4150 they read as nonsense. They are HINTS, greyed
 * in empty fields, never values: nothing is sent until the operator types.
 *
 * The shape is the old example's: a 2-point entry zone next to price and a
 * stop 9 beyond it. A BUY limit rests below the market and a SELL above, so
 * the zone and stop sit on the side the order actually belongs.
 */
import { describe, expect, it } from "vitest";
import { limitPlaceholders } from "../internal/limitPlaceholders";

describe("limitPlaceholders", () => {
  it("puts a BUY's zone just below the bid and its stop beneath", () => {
    expect(limitPlaceholders("BUY", { bid: 4150.4, ask: 4150.61 })).toEqual({
      entryLow: "4148.00", entryHigh: "4150.00", stopLoss: "4139.00",
    });
  });

  it("puts a SELL's zone just above the ask and its stop over it", () => {
    expect(limitPlaceholders("SELL", { bid: 4150.4, ask: 4150.61 })).toEqual({
      entryLow: "4151.00", entryHigh: "4153.00", stopLoss: "4162.00",
    });
  });

  it("says what goes in the box when there is no price to go on", () => {
    expect(limitPlaceholders("BUY", null)).toEqual({
      entryLow: "price", entryHigh: "price", stopLoss: "price",
    });
  });

  it("does not invent a number from a price that is not one", () => {
    expect(limitPlaceholders("BUY", { bid: Number.NaN, ask: 4150 }).entryLow).toBe("price");
  });
});
