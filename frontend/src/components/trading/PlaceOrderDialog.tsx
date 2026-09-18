import { TriangleAlert } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { usePlaceOrderDialogController } from "./hooks/usePlaceOrderDialogController";
import { OrderForm } from "./internal/OrderForm";

interface PlaceOrderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPlaced: () => void;
  /** Non-null when the backend says trading is not currently allowed. */
  disabledReason: string | null;
}

/**
 * Manual market order. **This button spends real money.**
 *
 * The rules it implements, from the frontend money conventions:
 * the confirmation names instrument, direction and size; it never defaults to
 * yes; a disabled control always shows its reason; a refusal is shown
 * verbatim.
 */
export function PlaceOrderDialog({
  open, onOpenChange, onPlaced, disabledReason,
}: PlaceOrderDialogProps) {
  const c = usePlaceOrderDialogController(onPlaced);

  const close = (next: boolean) => {
    if (!next) c.reset();
    onOpenChange(next);
  };

  return (
    <DialogShell
      open={open}
      onOpenChange={close}
      title="Market order"
      description="XAUUSD, placed immediately at the current price."
      footer={
        c.step === "form" ? (
          <>
            <Button variant="ghost" onClick={() => close(false)}>Cancel</Button>
            <Button
              variant={c.direction === "SELL" ? "danger" : "success"}
              onClick={c.review}
              disabledReason={disabledReason}
            >
              Review {c.direction}
            </Button>
          </>
        ) : (
          <>
            {/* Cancel is first and is the plain action; confirming is the
                deliberate one. Nothing here is focused by default. */}
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
        <OrderForm controller={c} />
      ) : (
        <div className="space-y-3">
          <p data-testid="order-summary" className="text-sm text-ink-1">{c.summary}</p>
          <p className="text-xs text-ink-3">
            The backend decides whether this order is allowed and at what size. If it refuses,
            the reason appears below.
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
            <p
              role="alert"
              className="rounded border border-loss/40 bg-loss/10 px-3 py-2 text-xs text-loss"
            >
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
