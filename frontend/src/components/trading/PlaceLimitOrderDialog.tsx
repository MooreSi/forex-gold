import { TriangleAlert } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { usePlaceLimitOrderController } from "./hooks/usePlaceLimitOrderController";
import { LimitOrderForm } from "./internal/LimitOrderForm";

interface PlaceLimitOrderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPlaced: () => void;
  disabledReason: string | null;
}

/**
 * A resting BuyLimit/SellLimit at the broker. **This spends real money** —
 * later, and without anybody watching, which is the part worth saying out loud
 * on the confirmation.
 */
export function PlaceLimitOrderDialog({
  open, onOpenChange, onPlaced, disabledReason,
}: PlaceLimitOrderDialogProps) {
  const c = usePlaceLimitOrderController(onPlaced);

  const close = (next: boolean) => {
    if (!next) c.reset();
    onOpenChange(next);
  };

  return (
    <DialogShell
      open={open}
      onOpenChange={close}
      title="Limit order"
      description="XAUUSD, resting at the broker until price reaches the zone."
      footer={
        c.step === "form" ? (
          <>
            <Button variant="ghost" onClick={() => close(false)}>Cancel</Button>
            <Button
              variant={c.direction === "SELL" ? "danger" : "success"}
              onClick={c.review}
              disabledReason={disabledReason ?? c.incomplete}
            >
              Review {c.direction}
            </Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={c.reset} disabled={c.step === "sending"}>
              Back
            </Button>
            <Button
              variant={c.direction === "SELL" ? "danger" : "success"}
              onClick={() => void c.send()}
              disabled={c.step === "sending"}
              disabledReason={disabledReason}
            >
              {c.step === "sending" ? "Placing…" : `Place this ${c.direction}`}
            </Button>
          </>
        )
      }
    >
      {c.step === "form" ? (
        <LimitOrderForm controller={c} />
      ) : (
        <div className="space-y-3">
          <p data-testid="limit-order-summary" className="text-sm text-ink-1">
            {c.summary}
          </p>
          <p className="text-xs text-warning">
            This order rests at the broker and can fill at any time, including
            while nothing is watching.
          </p>
          {c.refusal && (
            <p
              role="alert"
              className="flex items-start gap-2 rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning"
            >
              <TriangleAlert size={14} className="mt-0.5 shrink-0" />
              <span>{c.refusal}</span>
            </p>
          )}
          {c.failure && (
            <p role="alert" className="rounded border border-loss/40 bg-loss/10 px-3 py-2 text-xs text-loss">
              The order could not be sent: {c.failure}
            </p>
          )}
        </div>
      )}
      {disabledReason && (
        <p className="mt-3 text-xs text-warning" role="status">{disabledReason}</p>
      )}
    </DialogShell>
  );
}
