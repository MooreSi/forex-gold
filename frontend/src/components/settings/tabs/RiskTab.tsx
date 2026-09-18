import { EmptyState } from "@/components/shared/EmptyState";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsField } from "../internal/SettingsField";

const FIELDS: { key: string; label: string; hint: string }[] = [
  { key: "risk_pct", label: "Risk per trade (%)",
    hint: "Percentage of the account risked on each entry. The service clamps this." },
  { key: "max_open_trades", label: "Maximum open trades",
    hint: "New entries are refused once this many positions are open." },
  { key: "daily_loss_limit_pct", label: "Daily loss limit (%)",
    hint: "Trading halts for the day once losses reach this." },
  { key: "max_lot_size", label: "Maximum lot size",
    hint: "A hard ceiling applied after sizing, whatever the risk maths asks for." },
];

/**
 * The numbers that decide how much money a trade can lose.
 *
 * Every field shows what was STORED after a save, not what was typed: the risk
 * service clamps, and a field that kept the operator's number would be telling
 * them the engine is using a value it is not.
 */
export function RiskTab() {
  const risk = useSettingsResource<Record<string, unknown>>("/api/settings/risk");

  if (!risk.data) {
    return <EmptyState title={risk.error ? "Could not load the risk settings" : "Loading"} hint={risk.error ?? undefined} />;
  }
  return (
    <div className="space-y-3">
      <p className="rounded border border-warning/40 bg-warning/10 px-3 py-2 text-[11px] text-warning">
        These decide how much a single trade can lose. Every value is re-read
        from the backend after saving, so what you see is what the engine uses.
      </p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {FIELDS.map((f) => (
          <SettingsField
            key={f.key}
            label={f.label}
            hint={f.hint}
            type="number"
            version={risk.version}
            value={String(risk.data?.[f.key] ?? "")}
            onCommit={(v) => void risk.save({ [f.key]: Number(v) })}
          />
        ))}
      </div>
      {risk.error && (
        <p role="alert" className="text-xs text-loss">{risk.error}</p>
      )}
    </div>
  );
}
