export interface LivePrice {
  bid: number;
  ask: number;
}

/** Points between the entry zone's edges, and from its far edge to the stop.
 *  The shape of the old fixed example (2430 / 2432 / 2421). */
const ZONE = 2;
const STOP = 9;

/**
 * Example prices for the limit order form's empty fields, from the live price.
 *
 * Hints only (greyed placeholder text): nothing is filled in or sent until
 * the operator types. They were fixed at 2430 / 2432 / 2421 from when gold
 * traded near 2430 and read as nonsense at 4150 (owner, 2026-09-28). A BUY
 * limit rests below the market and a SELL above, so the example sits on the
 * side the order belongs.
 */
export function limitPlaceholders(
  direction: "BUY" | "SELL",
  price: LivePrice | null | undefined,
): { entryLow: string; entryHigh: string; stopLoss: string } {
  const ok = price && Number.isFinite(price.bid) && Number.isFinite(price.ask);
  if (!ok) return { entryLow: "price", entryHigh: "price", stopLoss: "price" };
  const f = (n: number) => n.toFixed(2);
  if (direction === "BUY") {
    const high = Math.floor(price.bid);
    return { entryLow: f(high - ZONE), entryHigh: f(high), stopLoss: f(high - ZONE - STOP) };
  }
  const low = Math.ceil(price.ask);
  return { entryLow: f(low), entryHigh: f(low + ZONE), stopLoss: f(low + ZONE + STOP) };
}
