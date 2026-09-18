import { cn } from "@/lib/cn";
import type { usePlaceLimitOrderController } from "../hooks/usePlaceLimitOrderController";

type Controller = ReturnType<typeof usePlaceLimitOrderController>;

function Field({
  label, value, onChange, placeholder, hint,
}: {
  label: string; value: string; onChange: (v: string) => void;
  placeholder: string; hint?: string;
}) {
  return (
    <label className="block">
      <span className="text-xs text-ink-2">{label}</span>
      <input
        aria-label={label}
        value={value}
        inputMode="decimal"
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="num mt-1 w-full rounded border border-line bg-surface-1 px-2 py-1.5 text-sm text-ink-1 placeholder:text-ink-3"
      />
      {hint && <span className="mt-1 block text-[11px] text-ink-3">{hint}</span>}
    </label>
  );
}

export function LimitOrderForm({ controller: c }: { controller: Controller }) {
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

      <div className="grid grid-cols-2 gap-3">
        <Field label="Entry zone low" value={c.entryLow} onChange={c.setEntryLow} placeholder="2430.00" />
        <Field label="Entry zone high" value={c.entryHigh} onChange={c.setEntryHigh} placeholder="2432.00" />
      </div>

      <Field
        label="Stop loss"
        value={c.stopLoss}
        onChange={c.setStopLoss}
        placeholder="2421.00"
        hint="Required. A resting order with no stop can fill and run unattended."
      />
      <Field
        label="Lots"
        value={c.lots}
        onChange={c.setLots}
        placeholder="auto"
        hint="Leave blank to size the trade from your risk settings."
      />

      <div>
        <span className="text-xs text-ink-2">Targets</span>
        <div className="mt-1 grid grid-cols-4 gap-2">
          {c.targets.map((value, i) => (
            <input
              key={i}
              aria-label={`TP${i + 1}`}
              value={value}
              inputMode="decimal"
              placeholder={`TP${i + 1}`}
              onChange={(e) => c.setTarget(i, e.target.value)}
              className="num rounded border border-line bg-surface-1 px-2 py-1 text-xs text-ink-1 placeholder:text-ink-3"
            />
          ))}
        </div>
        <span className="mt-1 block text-[11px] text-ink-3">
          All optional. Leave a level blank and the ladder simply stops there.
        </span>
      </div>

      <Field label="Notes" value={c.notes} onChange={c.setNotes} placeholder="" />
    </div>
  );
}
