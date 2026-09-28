import { useState } from "react";
import { Plus } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { PlaceLimitOrderDialog } from "@/components/trading/PlaceLimitOrderDialog";
import { PlaceOrderDialog } from "@/components/trading/PlaceOrderDialog";
import { useOrderGate } from "@/components/trading/hooks/useOrderGate";
import type { Tick } from "@/api/types";
import { positionLevels, type Drawing } from "./drawingGeometry";

type Open = "market" | "limit" | "position-market" | "position-limit" | null;

/**
 * Market and Limit order, from the Chart tab (owner, 2026-09-28: draw on the
 * chart, then decide to place a manual order).
 *
 * The Trading tab's own dialogs, opened from here: the same form, the same
 * review step and confirmation, the same backend refusal. Nothing about how
 * an order is built or sent differs from the Trading tab, and the buttons are
 * disabled for the same reasons, from the same `useOrderGate`.
 *
 * With a position drawing selected, two more buttons fill those dialogs from
 * it: direction, stop and target, and for a limit the entry. They fill; they
 * do not send. A fresh dialog is mounted for each, so what was drawn is what
 * the form starts with, and a plain Market order never inherits it.
 */
export function ChartOrderActions({
  onPlaced, tick, position,
}: { onPlaced: () => void; tick?: Tick | null; position?: Drawing | null }) {
  const { disabledReason } = useOrderGate();
  const [open, setOpen] = useState<Open>(null);
  const shut = (next: boolean) => { if (!next) setOpen(null); };
  const levels = position ? positionLevels(position.points) : null;
  const positionReason = disabledReason ?? (levels ? null
    : "This position is not a trade: the stop and target must be on opposite sides of the entry.");
  // Remount per drawing and per shape, so a moved position refills the form.
  const key = position ? `${position.id}:${JSON.stringify(position.points)}` : "none";

  return (
    <>
      {position && (
        <>
          <Button variant="success" onClick={() => setOpen("position-market")}
            disabledReason={positionReason}>
            <Plus size={13} /> Market from position
          </Button>
          <Button onClick={() => setOpen("position-limit")} disabledReason={positionReason}>
            <Plus size={13} /> Limit from position
          </Button>
        </>
      )}
      <Button variant="success" onClick={() => setOpen("market")} disabledReason={disabledReason}>
        <Plus size={13} /> Market order
      </Button>
      <Button onClick={() => setOpen("limit")} disabledReason={disabledReason}>
        <Plus size={13} /> Limit order
      </Button>
      <PlaceOrderDialog
        open={open === "market"}
        onOpenChange={shut}
        onPlaced={onPlaced}
        disabledReason={disabledReason}
      />
      <PlaceLimitOrderDialog
        open={open === "limit"}
        onOpenChange={shut}
        onPlaced={onPlaced}
        disabledReason={disabledReason}
        price={tick}
      />
      {open === "position-market" && levels && (
        <PlaceOrderDialog
          key={key}
          open
          onOpenChange={shut}
          onPlaced={onPlaced}
          disabledReason={disabledReason}
          prefill={{ direction: levels.direction, stopLoss: levels.stop, takeProfit: levels.target }}
        />
      )}
      {open === "position-limit" && levels && (
        <PlaceLimitOrderDialog
          key={key}
          open
          onOpenChange={shut}
          onPlaced={onPlaced}
          disabledReason={disabledReason}
          price={tick}
          prefill={{
            direction: levels.direction, entry: levels.entry,
            stopLoss: levels.stop, takeProfit: levels.target,
          }}
        />
      )}
    </>
  );
}
