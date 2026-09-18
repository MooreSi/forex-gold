import { useCallback, useMemo, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { MarketOrderRequest } from "@/api/types";

export type Direction = "BUY" | "SELL";
type Step = "form" | "confirm" | "sending";

/**
 * The state behind the manual market order.
 *
 * Two deliberate properties:
 *
 * * **Confirmation is a separate step, and it does not default to yes.** The
 *   dialog moves form → confirm → sending, and the confirm step restates the
 *   instrument, the direction and the size in words before anything is sent.
 * * **Blank means blank.** An empty stop-loss field stays `null` on the way to
 *   the API, because `null` is what tells the engine to compute an ATR stop
 *   through DPM. Substituting a number here would quietly take that decision
 *   away from the risk engine.
 */
export function usePlaceOrderDialogController(onPlaced: () => void) {
  const [step, setStep] = useState<Step>("form");
  const [direction, setDirection] = useState<Direction>("BUY");
  const [lots, setLots] = useState("");
  const [stopLoss, setStopLoss] = useState("");
  const [takeProfit, setTakeProfit] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const numeric = (v: string): number | null => {
    const t = v.trim();
    if (t === "") return null;
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
  };

  const request = useMemo<MarketOrderRequest>(
    () => ({
      direction,
      lot_size: numeric(lots),
      stop_loss: numeric(stopLoss),
      take_profit: numeric(takeProfit),
    }),
    [direction, lots, stopLoss, takeProfit],
  );

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
      await api.post("/api/trading/orders/market", request);
      onPlaced();
      reset();
      return true;
    } catch (e) {
      // A refusal is the backend's answer and the user must read it exactly.
      // Anything else is a failure of ours and says so separately, so the two
      // are never confused on screen.
      if (e instanceof ApiError && e.isRefusal) setRefusal(e.message);
      else setFailure(e instanceof Error ? e.message : String(e));
      setStep("confirm");
      return false;
    }
  }, [request, onPlaced, reset]);

  /** The sentence the confirm step shows. Instrument, direction and size —
   *  never just "Are you sure?". */
  const summary = useMemo(() => {
    const size = request.lot_size == null
      ? "a size calculated from your risk settings"
      : `${request.lot_size.toFixed(2)} lots`;
    const stop = request.stop_loss == null
      ? "a stop loss calculated by DPM"
      : `a stop loss at ${request.stop_loss.toFixed(2)}`;
    return `${direction} XAUUSD, ${size}, with ${stop}.`;
  }, [direction, request]);

  return {
    step, direction, setDirection, lots, setLots, stopLoss, setStopLoss,
    takeProfit, setTakeProfit, refusal, failure, request, summary,
    review, send, reset,
  };
}
