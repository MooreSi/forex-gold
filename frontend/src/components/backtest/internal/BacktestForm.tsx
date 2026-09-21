import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
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

/**
 * `hint` is the unit, printed under the box. `help` is the sentence that only
 * appears on hover — several of these change what the result MEANS rather than
 * how it is sized, and a unit alone does not say so.
 */
const NUMERIC: {
  key: NumericField; label: string; hint: string; help: string;
}[] = [
  { key: "starting_balance", label: "Starting balance", hint: "$",
    help: "The balance the simulated account opens with. Percentage risk is taken from the running balance, so this changes position sizes all the way through — not just the final number." },
  { key: "risk_pct", label: "Risk per trade", hint: "%",
    help: "How much of the simulated balance each entry risks. Ignored entirely when Fixed lots is not 0." },
  { key: "lots_per_trade", label: "Fixed lots", hint: "0 = size from risk",
    help: "Trade every signal at this size instead of sizing from risk. 0 hands sizing back to Risk per trade. Fixed lots make strategies comparable; risk sizing shows what the account would actually have done." },
  { key: "spread_pts", label: "Spread", hint: "points",
    help: "The spread charged on every entry and exit. Gold's real spread widens at the open and around news, so a single number here flatters strategies that trade at those moments." },
  { key: "commission_per_lot", label: "Commission", hint: "$ per lot",
    help: "Round-turn commission per lot, charged on every simulated trade. Leaving it at 0 is the most common way a losing strategy backtests as a winner." },
  { key: "max_sl_pts", label: "Max SL distance", hint: "points; wider is filtered out",
    help: "Signals whose stop is further away than this are dropped from the run rather than traded small. It filters the SIGNAL SET, so two runs with different values are not comparing the same trades." },
  { key: "split_fraction", label: "Out-of-sample split", hint: "0 = off",
    help: "Hold back this fraction of the history and report it separately, so a strategy tuned on the first part can be checked against data it never saw. 0 runs everything as one block." },
  { key: "days", label: "Days of history", hint: "candles are fetched for this window",
    help: "How far back to walk. A longer window costs a slower run and needs the candles to exist — the result says how many were actually loaded." },
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
          <Tooltip label="The candle size the strategies are walked over. It decides how precisely a stop or target can be seen being hit, not how much history is used.">
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
          </Tooltip>
        </label>
        <label className="text-xs text-ink-2">
          Data
          <Tooltip label="Ticks replay every price the broker printed, so a stop and a target inside one candle are ordered correctly. Candles are far faster but have to guess that order, which flatters strategies whose SL and TP are close together.">
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
          </Tooltip>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-ink-2">
          <Tooltip label="Walk only the signals this app actually opened a position from, ignoring the ones a gate refused. It answers 'would a different strategy have done better on the trades I took', not 'on every signal I received'.">
            <input
              type="checkbox"
              aria-label="Only signals that became real trades"
              checked={form.live_trades_only}
              onChange={(e) => set("live_trades_only", e.target.checked)}
              className="accent-accent"
            />
          </Tooltip>
          Only signals that became real trades
        </label>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {NUMERIC.map(({ key, label, hint, help }) => (
          <label key={key} className="block text-xs text-ink-2">
            {label}
            <Tooltip label={help}>
              <input
                aria-label={label}
                inputMode="decimal"
                value={form[key]}
                onChange={(e) => set(key, e.target.value)}
                className="num mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
              />
            </Tooltip>
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
