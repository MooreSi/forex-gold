import { useCallback, useMemo, useState } from "react";
import { api, ApiError } from "@/api/client";

export type Direction = "BUY" | "SELL";
type Step = "form" | "confirm" | "sending";

/**
 * A genuine resting limit order at the broker, not a watched price.
 *
 * Same two-step confirmation as the market order, and the same rule about
 * blanks: an empty field stays `null` so the engine keeps whatever decision it
 * would have made. The zone is the part that differs — a limit order has a
 * range rather than a price, and the confirmation says both edges.
 */
export function usePlaceLimitOrderController(onPlaced: () => void) {
  const [step, setStep] = useState<Step>("form");
  const [direction, setDirection] = useState<Direction>("BUY");
  const [entryLow, setEntryLow] = useState("");
  const [entryHigh, setEntryHigh] = useState("");
  const [stopLoss, setStopLoss] = useState("");
  const [targets, setTargets] = useState<string[]>(["", "", "", "", "", "", "", ""]);
  const [lots, setLots] = useState("");
  const [notes, setNotes] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const numeric = (v: string): number | null => {
    const t = v.trim();
    if (t === "") return null;
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
  };

  const request = useMemo(() => {
    const tps: Record<string, number | null> = {};
    targets.forEach((t, i) => {
      tps[`tp${i + 1}`] = numeric(t);
    });
    return {
      direction,
      entry_low: numeric(entryLow) ?? 0,
      entry_high: numeric(entryHigh) ?? 0,
      stop_loss: numeric(stopLoss) ?? 0,
      lot_size: numeric(lots),
      notes,
      ...tps,
    };
  }, [direction, entryLow, entryHigh, stopLoss, lots, notes, targets]);

  /** Why the form cannot be reviewed yet, in the user's words. A limit order
   *  with no zone and no stop is not an order. */
  const incomplete = useMemo(() => {
    if (numeric(entryLow) == null || numeric(entryHigh) == null) {
      return "Enter both edges of the entry zone.";
    }
    if (numeric(stopLoss) == null) {
      return "A resting order needs a stop loss.";
    }
    return null;
  }, [entryLow, entryHigh, stopLoss]);

  const reset = useCallback(() => {
    setStep("form");
    setRefusal(null);
    setFailure(null);
  }, []);

  const review = useCallback(() => {
    setRefusal(null);
    setFailure(null);
    setStep("confirm");
  }, []);

  const send = useCallback(async () => {
    setStep("sending");
    try {
      await api.post("/api/trading/orders/limit", request);
      onPlaced();
      reset();
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.isRefusal) setRefusal(e.message);
      else setFailure(e instanceof Error ? e.message : String(e));
      setStep("confirm");
      return false;
    }
  }, [request, onPlaced, reset]);

  const summary = useMemo(() => {
    const size = request.lot_size == null
      ? "a size calculated from your risk settings"
      : `${request.lot_size.toFixed(2)} lots`;
    return (
      `${direction} XAUUSD, ${size}, resting between ` +
      `${request.entry_low.toFixed(2)} and ${request.entry_high.toFixed(2)}, ` +
      `stop loss ${request.stop_loss.toFixed(2)}.`
    );
  }, [direction, request]);

  const setTarget = useCallback((index: number, value: string) => {
    setTargets((t) => t.map((v, i) => (i === index ? value : v)));
  }, []);

  return {
    step, direction, setDirection, entryLow, setEntryLow, entryHigh, setEntryHigh,
    stopLoss, setStopLoss, targets, setTarget, lots, setLots, notes, setNotes,
    refusal, failure, request, summary, incomplete, review, send, reset,
  };
}
