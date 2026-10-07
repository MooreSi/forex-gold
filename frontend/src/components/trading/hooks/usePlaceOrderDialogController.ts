import { useCallback, useMemo, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { MarketOrderRequest } from "@/api/types";
import { SINGLE_TP } from "../internal/orderStrategy";

export type Direction = "BUY" | "SELL";
type Step = "form" | "confirm" | "sending";

/** What a position drawn on the chart fills in (2026-09-28). Lots are not in
 *  it: they stay blank, so the risk settings size the order as they would a
 *  typed one. */
export interface MarketOrderPrefill {
  direction: Direction;
  stopLoss: number;
  takeProfit: number;
}

/** A price as the form shows it: two places, no trailing zeros. */
export const priceField = (n: number) => String(Math.round(n * 100) / 100);

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
 *
 * The strategy is the one exception to "blank means blank" (owner,
 * 2026-10-07): a blank strategy is sent as `SINGLE_TP`, a single take profit,
 * because the backend's own null is the global Scale Out + Breakeven, which
 * is what this dialog used to fall into without being asked.
 */
export function usePlaceOrderDialogController(
  onPlaced: () => void, initial?: MarketOrderPrefill | null,
) {
  const [step, setStep] = useState<Step>("form");
  const [direction, setDirection] = useState<Direction>(initial?.direction ?? "BUY");
  const [lots, setLots] = useState("");
  const [stopLoss, setStopLoss] = useState(initial ? priceField(initial.stopLoss) : "");
  const [takeProfit, setTakeProfit] = useState(initial ? priceField(initial.takeProfit) : "");
  const [strategy, setStrategy] = useState("");
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
      strategy: strategy || SINGLE_TP,
    }),
    [direction, lots, stopLoss, takeProfit, strategy],
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
    const target = request.take_profit == null
      ? ""
      : ` and a take profit at ${request.take_profit.toFixed(2)}`;
    return `${direction} XAUUSD, ${size}, with ${stop}${target}.`;
  }, [direction, request]);

  return {
    step, direction, setDirection, lots, setLots, stopLoss, setStopLoss,
    takeProfit, setTakeProfit, strategy, setStrategy, refusal, failure, request, summary,
    review, send, reset,
  };
}
