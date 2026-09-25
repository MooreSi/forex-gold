import { cn } from "@/lib/cn";
import { formatLots, formatPercent } from "@/components/shared/format";
import { SettingsField } from "@/components/settings/internal/SettingsField";
import { SettingsToggle } from "@/components/settings/internal/SettingsToggle";

interface PerTradeSizingSectionProps {
  data: Record<string, unknown>;
  version: number;
  save: (body: Record<string, unknown>) => Promise<void>;
}

const num = (v: unknown) => {
  const n = Number(v ?? 0);
  return Number.isFinite(n) ? n : 0;
};

/**
 * How big one trade is: a risk % OR a fixed lot size, never both.
 *
 * docs/todo/risk/010. On 2026-09-24 Gold Diggers VIP sent MT5 two orders for 0
 * lots. The screen showed Risk per trade 2% while a fixed lot of 0.1 -- a
 * setting this screen did not show at all -- was what sized every plain
 * channel, and each EA template sized itself from its own lots.
 *
 * So the choice is made here, visibly, and the field not in use is greyed out
 * with its value kept. Fixed lots is live exactly when `strategy_lot_size` is
 * above 0, which is how every order path reads it; choosing Risk % stores 0
 * there and parks the lot in `strategy_lot_size_parked`, so switching back
 * restores it.
 *
 * The EA template override makes templates obey this size too. ORB and Set &
 * Forget size themselves and are never affected; a lot typed on an order is
 * never replaced.
 */
export function PerTradeSizingSection({ data, version, save }: PerTradeSizingSectionProps) {
  const fixedLot = num(data.strategy_lot_size);
  const parkedLot = num(data.strategy_lot_size_parked);
  const riskPct = num(data.risk_per_trade_pct);
  const maxLot = num(data.max_lot_size);
  const byLots = fixedLot > 0;
  const override = Boolean(num(data.global_sizing_override));

  const chooseRisk = () => {
    if (!byLots) return;
    void save({ strategy_lot_size: 0, strategy_lot_size_parked: fixedLot });
  };
  const chooseLots = () => {
    if (byLots) return;
    // The lot kept from last time, or the smallest the broker takes. Never a
    // size nobody chose: 0.01 is the least a trade can risk.
    void save({ strategy_lot_size: parkedLot > 0 ? parkedLot : 0.01 });
  };

  const sizeLine = byLots
    ? `Each trade opens ${formatLots(fixedLot)} lots, whatever its stop distance.`
    : `Each trade risks ${formatPercent(riskPct, 2)} of the account balance, sized from its stop.`;

  const templateLine = override
    ? `EA template channels use this size too, and their own lot sizes are ignored.${
      byLots ? "" : " On a grid template the percentage is the total, split across its legs."}`
    : "EA template channels use the lot size set on their template. Every other channel uses this size.";

  return (
    <div data-testid="per-trade-sizing" className="space-y-3 rounded border border-line p-3">
      <div className="flex flex-wrap items-center gap-3">
        <span id="sizing-mode-label" className="text-xs text-ink-2">Size each trade by</span>
        <div role="radiogroup" aria-labelledby="sizing-mode-label"
          className="inline-flex overflow-hidden rounded border border-line">
          {([["risk", "Risk %", !byLots, chooseRisk], ["lots", "Fixed lots", byLots, chooseLots]] as const)
            .map(([key, label, on, choose]) => (
              <button
                key={key}
                type="button"
                role="radio"
                aria-checked={on}
                onClick={choose}
                className={cn(
                  "px-3 py-1 text-xs transition-colors",
                  on ? "bg-accent/15 font-semibold text-ink-1"
                    : "bg-surface-1 text-ink-3 hover:bg-surface-2 hover:text-ink-2",
                )}
              >
                {label}
              </button>
            ))}
        </div>
      </div>

      <div className="grid items-start gap-3 sm:grid-cols-2">
        <div data-testid="risk-risk_per_trade_pct">
          <SettingsField
            label="Risk per trade (%)"
            hint={byLots ? "Not in use while sizing by fixed lots." : "Percentage of the account risked on each entry."}
            type="number"
            version={version}
            disabled={byLots}
            value={String(data.risk_per_trade_pct ?? "")}
            onCommit={(v) => void save({ risk_per_trade_pct: Number(v) })}
          />
        </div>
        <div data-testid="risk-strategy_lot_size">
          <SettingsField
            label="Fixed lot size"
            hint={byLots ? "Lots on every entry, per leg on a grid." : "Not in use while sizing by risk %."}
            type="number"
            version={version}
            disabled={!byLots}
            value={String(byLots ? fixedLot : parkedLot || "")}
            onCommit={(v) => {
              const lot = Number(v);
              // Clearing the box is choosing Risk %, and keeps the lot parked.
              void save(lot > 0
                ? { strategy_lot_size: lot }
                : { strategy_lot_size: 0, strategy_lot_size_parked: fixedLot });
            }}
          />
        </div>
      </div>

      <p data-testid="sizing-summary" className="text-[11px] text-ink-1">{sizeLine}</p>

      <div data-testid="risk-global_sizing_override"
        className={cn("rounded border px-2 py-2", override ? "border-accent/50 bg-accent/10" : "border-line")}>
        <SettingsToggle
          label="EA template override"
          hint="Size every automated trade, market or limit, from this setting instead of the EA template's own lots and risk %. ORB and Set & Forget keep their own sizing."
          checked={override}
          onChange={(v) => void save({ global_sizing_override: v ? 1 : 0 })}
        />
        <p data-testid="sizing-template-line" className="mt-1 pl-5 text-[11px] text-ink-2">{templateLine}</p>
      </div>

      {maxLot < 0.01 && (
        <p role="alert" className="text-[11px] text-loss">
          Maximum lot size is {maxLot}. Every trade sizes to 0 lots and is refused until it is at least 0.01.
        </p>
      )}
    </div>
  );
}
