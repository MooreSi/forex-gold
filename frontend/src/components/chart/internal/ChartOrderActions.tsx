import { useState } from "react";
import { Plus } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { PlaceLimitOrderDialog } from "@/components/trading/PlaceLimitOrderDialog";
import { PlaceOrderDialog } from "@/components/trading/PlaceOrderDialog";
import { useOrderGate } from "@/components/trading/hooks/useOrderGate";
import type { Tick } from "@/api/types";

/**
 * Market and Limit order, from the Chart tab (owner, 2026-09-28: draw on the
 * chart, then decide to place a manual order).
 *
 * The Trading tab's own dialogs, opened from here: the same form, the same
 * review step and confirmation, the same backend refusal. Nothing about how
 * an order is built or sent differs from the Trading tab, and the buttons are
 * disabled for the same reasons, from the same `useOrderGate`.
 */
export function ChartOrderActions({
  onPlaced, tick,
}: { onPlaced: () => void; tick?: Tick | null }) {
  const { disabledReason } = useOrderGate();
  const [market, setMarket] = useState(false);
  const [limit, setLimit] = useState(false);

  return (
    <>
      <Button variant="success" onClick={() => setMarket(true)} disabledReason={disabledReason}>
        <Plus size={13} /> Market order
      </Button>
      <Button onClick={() => setLimit(true)} disabledReason={disabledReason}>
        <Plus size={13} /> Limit order
      </Button>
      <PlaceOrderDialog
        open={market}
        onOpenChange={setMarket}
        onPlaced={onPlaced}
        disabledReason={disabledReason}
      />
      <PlaceLimitOrderDialog
        open={limit}
        onOpenChange={setLimit}
        onPlaced={onPlaced}
        disabledReason={disabledReason}
        price={tick}
      />
    </>
  );
}
