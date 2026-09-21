import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import type { usePlaceOrderDialogController } from "../hooks/usePlaceOrderDialogController";

type Controller = ReturnType<typeof usePlaceOrderDialogController>;

function Field({
  label, value, onChange, placeholder, hint,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  hint: string;
}) {
  return (
    <label className="block">
      <span className="text-xs text-ink-2">{label}</span>
      {/* The hint is BOTH under the field and on hover: under it for somebody
          reading the form, on hover for somebody already typing in the box. */}
      <Tooltip label={hint}>
      <input
        value={value}
        inputMode="decimal"
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="num mt-1 w-full rounded border border-line bg-surface-1 px-2 py-1.5 text-sm text-ink-1 placeholder:text-ink-3"
      />
      </Tooltip>
      <span className="mt-1 block text-[11px] text-ink-3">{hint}</span>
    </label>
  );
}

export function OrderForm({ controller: c }: { controller: Controller }) {
  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        {(["BUY", "SELL"] as const).map((d) => (
          <button
            key={d}
            onClick={() => c.setDirection(d)}
            aria-pressed={c.direction === d}
            className={cn(
              "flex-1 rounded border px-3 py-2 text-sm font-semibold transition-colors",
              c.direction === d && d === "BUY" && "border-profit bg-profit/15 text-profit",
              c.direction === d && d === "SELL" && "border-loss bg-loss/15 text-loss",
              c.direction !== d && "border-line bg-surface-1 text-ink-3 hover:text-ink-2",
            )}
          >
            {d}
          </button>
        ))}
      </div>
      <Field
        label="Lots"
        value={c.lots}
        onChange={c.setLots}
        placeholder="auto"
        hint="Leave blank to size the trade from your risk settings."
      />
      <Field
        label="Stop loss"
        value={c.stopLoss}
        onChange={c.setStopLoss}
        placeholder="auto"
        hint="Leave blank to let DPM calculate an ATR-based stop. With DPM off, a stop is required."
      />
      <Field
        label="Take profit"
        value={c.takeProfit}
        onChange={c.setTakeProfit}
        placeholder="none"
        hint="Optional. Leave blank for no take profit."
      />
    </div>
  );
}
