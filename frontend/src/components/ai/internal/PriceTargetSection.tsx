import { formatMoney } from "@/components/shared/format";
import type { MarketAnalysis } from "../hooks/useMarketResearch";

function Levels({ label, values, tone }: {
  label: string; values: number[]; tone: string;
}) {
  if (values.length === 0) return null;
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] uppercase tracking-wide text-ink-3">{label}</span>
      {/* Three is what the NiceGUI card showed. A model asked for levels will
          happily return ten, and a column of ten is not a level to watch. */}
      {values.slice(0, 3).map((v) => (
        <span key={v} className={`num text-[11px] ${tone}`}>{formatMoney(v)}</span>
      ))}
    </div>
  );
}

/**
 * Where the model expects price to go today, and the levels it is watching.
 *
 * Rendered only when there is a range. A card showing $0.00 — $0.00 is a
 * price target as far as anyone reading it quickly is concerned.
 */
export function PriceTargetSection({ analysis }: { analysis: MarketAnalysis }) {
  const low = Number(analysis.price_low ?? 0);
  const high = Number(analysis.price_high ?? 0);
  const supports = (analysis.support_levels ?? []).filter((v) => Number(v) > 0);
  const resistances = (analysis.resistance_levels ?? []).filter((v) => Number(v) > 0);
  if (!(low > 0 || high > 0)) return null;

  return (
    <div
      data-testid="price-target"
      className="rounded-lg border border-line bg-surface-2 p-4"
    >
      <h3 className="text-[10px] font-semibold uppercase tracking-wider text-ink-3">
        Today&apos;s price target
      </h3>
      <div className="mt-1.5 flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-0">
          <span className="text-[10px] text-ink-3">LOW</span>
          <span className="num text-xl font-bold text-loss">{formatMoney(low)}</span>
        </div>
        <span className="pb-1 text-ink-3">—</span>
        <div className="flex flex-col gap-0">
          <span className="text-[10px] text-ink-3">HIGH</span>
          <span className="num text-xl font-bold text-profit">{formatMoney(high)}</span>
        </div>
        {high > low && (
          <span className="num pb-1 text-[11px] text-ink-3">
            Range: {formatMoney(high - low)}
          </span>
        )}
      </div>
      {(supports.length > 0 || resistances.length > 0) && (
        <div
          data-testid="levels"
          className="mt-2 grid grid-cols-2 gap-2 border-t border-line pt-2"
        >
          <Levels label="Support" values={supports} tone="text-profit" />
          <Levels label="Resistance" values={resistances} tone="text-loss" />
        </div>
      )}
    </div>
  );
}
