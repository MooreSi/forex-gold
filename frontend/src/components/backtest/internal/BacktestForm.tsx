import { Button } from "@/components/shared/Button";
import { cn } from "@/lib/cn";
import type { BacktestOptions } from "@/api/types";
import type {
  BacktestForm as FormValues, NumericField,
} from "../hooks/useBacktestController";

interface BacktestFormProps {
  options: BacktestOptions;
  form: FormValues;
  set: <K extends keyof FormValues>(key: K, value: FormValues[K]) => void;
  selected: string[];
  toggle: (key: string) => void;
  running: boolean;
  onRun: () => void;
}

const NUMERIC: { key: NumericField; label: string; hint: string }[] = [
  { key: "starting_balance", label: "Starting balance", hint: "$" },
  { key: "risk_pct", label: "Risk per trade", hint: "%" },
  { key: "lots_per_trade", label: "Fixed lots", hint: "0 = size from risk" },
  { key: "spread_pts", label: "Spread", hint: "points" },
  { key: "commission_per_lot", label: "Commission", hint: "$ per lot" },
  { key: "max_sl_pts", label: "Max SL distance", hint: "points; wider is filtered out" },
  { key: "split_fraction", label: "Out-of-sample split", hint: "0 = off" },
  { key: "days", label: "Days of history", hint: "candles are fetched for this window" },
];

function templateKey(row: Record<string, unknown>): string {
  return `template:${String(row["name"] ?? "")}`;
}

export function BacktestForm({
  options, form, set, selected, toggle, running, onRun,
}: BacktestFormProps) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-ink-2">
          Timeframe
          <select
            aria-label="Timeframe"
            value={form.timeframe}
            onChange={(e) => set("timeframe", e.target.value)}
            className="num ml-2 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
          >
            {options.timeframes.map((tf) => (
              <option key={tf} value={tf}>{tf}</option>
            ))}
          </select>
        </label>
        <label className="text-xs text-ink-2">
          Data
          <select
            aria-label="Data"
            value={form.granularity}
            onChange={(e) => set("granularity", e.target.value)}
            className="ml-2 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
          >
            {options.granularities.map((g) => (
              <option key={g} value={g}>{g === "ticks" ? "Ticks" : "Candles"}</option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-ink-2">
          <input
            type="checkbox"
            aria-label="Only signals that became real trades"
            checked={form.live_trades_only}
            onChange={(e) => set("live_trades_only", e.target.checked)}
            className="accent-accent"
          />
          Only signals that became real trades
        </label>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {NUMERIC.map(({ key, label, hint }) => (
          <label key={key} className="block text-xs text-ink-2">
            {label}
            <input
              aria-label={label}
              inputMode="decimal"
              value={form[key]}
              onChange={(e) => set(key, e.target.value)}
              className="num mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
            />
            <span className="mt-0.5 block text-[10px] text-ink-3">{hint}</span>
          </label>
        ))}
      </div>

      <div>
        <h3 className="mb-1.5 text-xs font-semibold text-ink-1">Strategies to compare</h3>
        <div className="flex flex-wrap gap-1.5">
          {options.strategies.map((row) => {
            const key = String(row["key"] ?? row["id"] ?? "");
            return (
              <button
                key={key}
                onClick={() => toggle(key)}
                aria-pressed={selected.includes(key)}
                className={cn(
                  "rounded border px-2 py-1 text-[11px] transition-colors",
                  selected.includes(key)
                    ? "border-accent bg-accent/15 text-ink-1"
                    : "border-line bg-surface-1 text-ink-3 hover:text-ink-2",
                )}
              >
                {String(row["name"] ?? key)}
              </button>
            );
          })}
        </div>

        {options.templates.length > 0 && (
          <>
            <h3 className="mb-1.5 mt-3 text-xs font-semibold text-ink-1">EA templates</h3>
            <div className="flex flex-wrap gap-1.5">
              {options.templates.map((row) => {
                const key = templateKey(row);
                const supported = row["supported"] !== false;
                const reason = String(row["reason"] ?? "");
                return (
                  <button
                    key={key}
                    onClick={() => supported && toggle(key)}
                    aria-pressed={selected.includes(key)}
                    disabled={!supported}
                    // A template that cannot be simulated says why, here, rather
                    // than being offered and then returning zeros.
                    title={supported ? undefined : reason}
                    className={cn(
                      "rounded border px-2 py-1 text-[11px] transition-colors",
                      !supported && "cursor-not-allowed border-line/50 text-ink-3/50",
                      supported && selected.includes(key)
                        ? "border-accent bg-accent/15 text-ink-1"
                        : supported && "border-line bg-surface-1 text-ink-3 hover:text-ink-2",
                    )}
                  >
                    {String(row["name"] ?? key)}
                    {!supported && <span className="ml-1 text-[9px]">not simulatable</span>}
                  </button>
                );
              })}
            </div>
          </>
        )}
      </div>

      <Button
        variant="primary"
        onClick={onRun}
        disabledReason={selected.length === 0 ? "Choose at least one strategy to compare." : null}
        disabled={running}
      >
        {running ? "Walking the history…" : "Run backtest"}
      </Button>
    </div>
  );
}
