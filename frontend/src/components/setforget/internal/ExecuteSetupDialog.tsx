import { TriangleAlert } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { formatMoney, formatPrice } from "@/components/shared/format";
import type { SetForgetCandidate } from "@/api/types";
import { waitFor } from "./waitFor";

interface ExecuteSetupDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  candidate: SetForgetCandidate;
  lots: number | null;
  riskMoney: number | null;
  rewardMoney: number | null;
  controlTarget: string;
  busy: boolean;
  onConfirm: () => void;
}

/**
 * The last thing between the setup and a real position.
 *
 * It names every number that is about to be sent, and says so explicitly:
 * these are the figures on screen, not a fresh reading. The chart moves as
 * price does, and re-reading on the way to the broker would place a trade
 * against numbers nobody ever saw. Same rule the ORB card follows, for the
 * same reason.
 *
 * The confirm button spells out the direction and the order type rather than
 * saying "Confirm". A button that says what it does cannot be pressed by
 * muscle memory on the wrong screen.
 */
export function ExecuteSetupDialog(props: ExecuteSetupDialogProps) {
  const {
    open, onOpenChange, candidate, lots, riskMoney, rewardMoney,
    controlTarget, busy, onConfirm,
  } = props;
  const resting = candidate.order_type === "limit";
  const what = resting
    ? `Rest a ${candidate.direction} limit at ${formatPrice(candidate.entry)}?`
    : `Open ${candidate.direction} at market?`;

  return (
    <DialogShell open={open} onOpenChange={onOpenChange} title={what}>
      <div className="space-y-3 text-xs text-ink-2">
        <p className="flex gap-2 rounded border border-warning/40 bg-warning/10 px-2.5
                      py-2 text-warning">
          <TriangleAlert size={14} className="mt-px shrink-0" />
          <span>
            This places a real order on the{" "}
            {controlTarget === "remote"
              ? "remote node"
              : "account this app is pointed at"}
            .
          </span>
        </p>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 rounded border border-line
                       bg-surface-2/50 px-3 py-2">
          <Row label="Direction" value={candidate.direction} />
          <Row label="Order" value={resting ? "Limit (rests at the zone)" : "Market"} />
          <Row
            label="Entry"
            value={resting
              ? `${formatPrice(candidate.entry)}  (${waitFor(candidate.distance_days)})`
              : formatPrice(candidate.entry)}
          />
          <Row label="Lots" value={lots ? lots.toFixed(2) : "sized from risk %"} />
          <Row
            label="Stop loss"
            value={formatPrice(candidate.stop_loss)
              + (riskMoney != null ? `  (${formatMoney(riskMoney)})` : "")}
          />
          <Row
            label="Take profit"
            value={formatPrice(candidate.take_profit)
              + (rewardMoney != null ? `  (${formatMoney(rewardMoney)})` : "")}
          />
        </dl>

        <p className="text-ink-3">
          These are the numbers shown on the page, not a fresh reading — the
          chart moves as price does.
          {resting && " A resting order fills only if price returns to the zone;"
            + " nothing happens until it does."}
        </p>

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            variant={candidate.direction === "BUY" ? "success" : "danger"}
            disabled={busy}
            onClick={onConfirm}
          >
            {busy
              ? "Placing…"
              : resting
                ? `Place ${candidate.direction} limit`
                : `Open ${candidate.direction}`}
          </Button>
        </div>
      </div>
    </DialogShell>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-ink-3">{label}</dt>
      <dd className="num text-right text-ink-1">{value}</dd>
    </>
  );
}
